"""A run service process: one event loop running many items at once (`Executor`).

- It runs up to `run_service_concurrency` items at once, of which background ones may take all
  but `run_service_interactive_reserve`: those slots are kept for people waiting in chat.
- It claims when it has room: woken by a Redis channel per lane as work is enqueued
  (`aios.run_service.queue.wake`), and every `run_service_idle_poll_seconds` regardless.
- It extends its items' leases every third of a lease, so only a dead process's lapse.
- An item's handler (`aios.run_service.work`) says it's done, with what follows it, or that
  it can't run yet. One that raises is handed back to be tried again, counting the attempt.
- With `scheduler_enabled`, it fires due schedules (`aios.scheduler.tick`) every
  `scheduler_interval_seconds`; and it runs the requeue sweep (`aios.run_service.sweep`).
- On a termination signal it stops claiming, lets its items finish for up to
  `run_service_shutdown_grace_seconds`, then cancels the rest and hands them back to the queue
  without counting the attempt.

`drain_once` runs every ready item to completion in-process (and what they enqueue), for tests.
"""

import asyncio
import contextlib
import logging
import signal

import redis
import redis.asyncio as aioredis
from anyio import CancelScope
from fastapi.concurrency import run_in_threadpool

from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import WorkLane
from aios.run_service import queue, work

log = logging.getLogger(__name__)

# How long an item whose handler raised waits before it's tried again.
RETRY_SECONDS = 10.0
# How often the requeue sweep runs.
SWEEP_SECONDS = 30.0


def _claim(limit: int, background_limit: int) -> list[queue.Claimed]:
    with SessionLocal() as db:
        return queue.claim(db, limit, background_limit)


def _complete(item: queue.Claimed, then: list[queue.Work]) -> None:
    with SessionLocal() as db:
        queue.complete(db, item, then)
    if then:
        queue.wake(*{w.lane for w in then})


def _release(item: queue.Claimed, delay_seconds: float, count_attempt: bool) -> None:
    with SessionLocal() as db:
        queue.release(db, item, delay_seconds=delay_seconds, count_attempt=count_attempt)


def _extend(items: list[queue.Claimed]) -> None:
    with SessionLocal() as db:
        queue.extend(db, items)


class Executor:
    """One process's items in flight, and its room for more."""

    def __init__(self, concurrency: int | None = None, reserve: int | None = None) -> None:
        settings = get_settings()
        self.concurrency = concurrency or settings.run_service_concurrency
        reserve = settings.run_service_interactive_reserve if reserve is None else reserve
        self.reserve = min(reserve, self.concurrency)
        self.running: dict[int, tuple[queue.Claimed, asyncio.Task]] = {}
        # Set when an item finishes, so the claim loop looks for more.
        self.changed = asyncio.Event()

    def room(self) -> tuple[int, int]:
        """How many more items it may claim, and how many of those may be background ones."""
        free = self.concurrency - len(self.running)
        background = sum(item.lane == WorkLane.background for item, _ in self.running.values())
        return free, max(0, min(free, self.concurrency - self.reserve - background))

    async def claim(self) -> list[asyncio.Task]:
        """Claim what there's room for, and start each in a task of its own."""
        free, background = self.room()
        if free <= 0:
            return []
        items = await run_in_threadpool(_claim, free, background)
        return [self._start(item) for item in items]

    def _start(self, item: queue.Claimed) -> asyncio.Task:
        task = asyncio.create_task(self._run(item), name=f"work-{item.kind}-{item.id}")
        self.running[item.id] = (item, task)

        def done(_: asyncio.Task) -> None:
            self.running.pop(item.id, None)
            self.changed.set()

        task.add_done_callback(done)
        return task

    async def _run(self, item: queue.Claimed) -> None:
        try:
            if item.exhausted:
                await work.fail(item)
                outcome: work.Outcome = work.Done()
            else:
                outcome = await work.HANDLERS[item.kind](item)
        except asyncio.CancelledError:
            # Stopping: back to the queue for another process, not counted as an attempt.
            with CancelScope(shield=True):
                await run_in_threadpool(_release, item, 0, False)
            raise
        except Exception:
            log.exception("work item %s (%s) failed; it'll be tried again", item.id, item.kind)
            await run_in_threadpool(_release, item, RETRY_SECONDS, True)
            return
        if isinstance(outcome, work.Later):
            await run_in_threadpool(_release, item, outcome.delay_seconds, False)
        else:
            await run_in_threadpool(_complete, item, outcome.then)

    async def extend_leases(self) -> None:
        if items := [item for item, _ in self.running.values()]:
            await run_in_threadpool(_extend, items)

    async def extend_forever(self) -> None:
        interval = get_settings().queue_lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                await self.extend_leases()
            except Exception:
                # Only a lapsed lease counts: a missed extension or two doesn't.
                log.exception("couldn't extend the run service's leases")

    async def stop(self, grace: float) -> None:
        """Let the items in flight finish for up to `grace` seconds, then cancel the rest (each
        handed back to the queue)."""
        tasks = [task for _, task in self.running.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def drain_once(concurrency: int | None = None, reserve: int | None = None) -> None:
    """Run every ready item to completion in this process, and what they enqueue as they finish,
    until nothing ready is left. Items handed back for later aren't waited for."""
    executor = Executor(concurrency, reserve)
    extending = asyncio.create_task(executor.extend_forever())
    try:
        while True:
            await executor.claim()
            if not executor.running:
                return
            executor.changed.clear()
            await executor.changed.wait()
    finally:
        extending.cancel()
        await asyncio.gather(extending, return_exceptions=True)


async def _listen(woken: asyncio.Event) -> None:
    """Set `woken` whenever work is enqueued in any lane; reconnects if Redis goes away (the
    poll covers the gap)."""
    channels = [queue.wake_channel(lane) for lane in WorkLane]
    while True:
        client = aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        try:
            async with client.pubsub(ignore_subscribe_messages=True) as pubsub:
                await pubsub.subscribe(*channels)
                async for _ in pubsub.listen():
                    woken.set()
        except (redis.RedisError, OSError) as exc:
            log.warning("run service wake channel lost: %s", exc)
        finally:
            with contextlib.suppress(redis.RedisError, OSError):
                await client.aclose()
        await asyncio.sleep(get_settings().run_service_idle_poll_seconds)


async def _every(interval: float, name: str, step) -> None:
    while True:
        try:
            await step()
        except Exception:
            log.exception("run service %s failed", name)
        await asyncio.sleep(interval)


async def run_forever(stopping: asyncio.Event | None = None) -> None:
    """Run the run service in this process until `stopping` is set (or a termination signal
    arrives), then stop gracefully."""
    from aios import scheduler
    from aios.run_service import sweep

    settings = get_settings()
    stopping = stopping or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stopping.set)
    executor = Executor()
    woken = asyncio.Event()
    helpers = [
        asyncio.create_task(_listen(woken), name="wake"),
        asyncio.create_task(executor.extend_forever(), name="leases"),
        asyncio.create_task(_every(SWEEP_SECONDS, "requeue sweep", sweep.run), name="sweep"),
    ]
    if settings.scheduler_enabled:
        helpers.append(
            asyncio.create_task(
                _every(settings.scheduler_interval_seconds, "scheduler tick", scheduler.tick),
                name="scheduler",
            )
        )
    log.info("run service started: %s slots", executor.concurrency)
    try:
        while not stopping.is_set():
            try:
                await executor.claim()
            except Exception:
                log.exception("run service claim failed")
            woken.clear()
            executor.changed.clear()
            waits = [
                asyncio.create_task(woken.wait()),
                asyncio.create_task(executor.changed.wait()),
                asyncio.create_task(stopping.wait()),
            ]
            await asyncio.wait(
                waits,
                timeout=settings.run_service_idle_poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in waits:
                task.cancel()
    finally:
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        await executor.stop(settings.run_service_shutdown_grace_seconds)
        log.info("run service stopped")
