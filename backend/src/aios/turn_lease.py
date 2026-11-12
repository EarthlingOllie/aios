"""The turn lease: how a run's turns take turns, wherever they run (two run service processes, a
schedule and a chat), so each starts from the history the previous one left rather than both
forking from the same checkpoint.

It's a lease on the run's row in its organisation's store (`agent_runs.lease_token`,
`lease_until`), not a database session lock, so it works through a connection pooler in
transaction mode. A run is named by its store and its id there, as ids repeat across stores; so
are its Redis channels.

- **Acquire.** A conditional update that succeeds only if the lease is free or expired, and sets
  a new token and a time it lasts until (`turn_lease_seconds`).
- **Wait.** On the run's release channel in Redis, with a one-second poll as a fallback (Redis
  down, or a release missed), for up to `turn_wait_seconds`. Past it, `RunBusy`: the run service
  hands the turn's item back, to be tried again a little later.
- **Refresh.** While the turn holds it (`held`), a heartbeat extends it while the token matches.
- **Release.** Cleared while the token matches, then the release channel is published.
- **Fencing.** Every checkpoint write first checks that the token still holds, in the write's
  own transaction (`aios.checkpoints.FencedSaver`), and fails if it doesn't: a process that lost
  its lease (frozen past its expiry, or stopped) can't write the run's history.
- **Stopping turns** (`stop`). Clears the token and publishes a stop signal, which the holding
  process acts on by cancelling the turn. If the signal never arrives (Redis down), the turn's
  next write fails on fencing, and its heartbeat finds the lease gone.
- **Turns underway** (`underway`) are the unexpired leases.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta

import redis
import redis.asyncio as aioredis
from anyio import CancelScope
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.models import AgentRun

log = logging.getLogger(__name__)

BUSY = "This run is busy"
STOPPED = "This turn was stopped."


class RunBusy(Exception):
    """Another turn of the run held its lease for longer than a turn waits."""

    def __init__(self) -> None:
        super().__init__(BUSY)


class RunGone(Exception):
    """A turn of a run that has been deleted."""


class LeaseLost(Exception):
    """A write by a turn whose lease no longer holds (it lapsed, or the turn was stopped)."""


def released_channel(data_store_id: uuid.UUID, run_id: int) -> str:
    return f"aios:turn-lease:{data_store_id}:{run_id}:released"


def stop_channel(data_store_id: uuid.UUID, run_id: int) -> str:
    return f"aios:turn-lease:{data_store_id}:{run_id}:stop"


@dataclass(eq=False)
class Lease:
    """A turn's hold on its run (in the store `data_store_id`). `on_stop` is what the holder
    does when the turn is stopped (cancel it); `stop` calls it once."""

    data_store_id: uuid.UUID
    run_id: int
    token: uuid.UUID
    on_stop: Callable[[], object] | None = None
    stopped: bool = False

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.on_stop is not None:
            self.on_stop()


def _lasts() -> timedelta:
    return timedelta(seconds=get_settings().turn_lease_seconds)


def _holding(lease: Lease):
    return (AgentRun.id == lease.run_id, AgentRun.lease_token == lease.token)


def try_acquire(data_store_id: uuid.UUID, run_id: int) -> Lease | None:
    """The run's lease, if it's free or expired; None if another turn holds it. Raises
    `RunGone` if the run has been deleted."""
    token = uuid.uuid4()
    with store_session(data_store_id) as db:
        got = db.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                or_(AgentRun.lease_token.is_(None), AgentRun.lease_until <= func.now()),
            )
            # Not a change to the run itself, so its `updated_at` stays (listings sort by it).
            .values(
                lease_token=token,
                lease_until=func.now() + _lasts(),
                updated_at=AgentRun.updated_at,
            )
            .returning(AgentRun.id)
        ).first()
        exists = got is not None or db.scalar(select(AgentRun.id).where(AgentRun.id == run_id))
        db.commit()
    if not exists:
        raise RunGone(f"Agent run {run_id} has been deleted")
    return Lease(data_store_id, run_id, token) if got is not None else None


def refresh(lease: Lease) -> bool:
    """Extend the lease while it's still this turn's; whether it was."""
    with store_session(lease.data_store_id) as db:
        got = db.execute(
            update(AgentRun)
            .where(*_holding(lease))
            .values(lease_until=func.now() + _lasts(), updated_at=AgentRun.updated_at)
            .returning(AgentRun.id)
        ).first()
        db.commit()
    return got is not None


def holds(lease: Lease) -> bool:
    """Whether the lease is still this turn's, and unexpired."""
    with store_session(lease.data_store_id) as db:
        return (
            db.scalar(
                select(AgentRun.id).where(*_holding(lease), AgentRun.lease_until > func.now())
            )
            is not None
        )


def _clear(lease: Lease) -> None:
    with store_session(lease.data_store_id) as db:
        db.execute(
            update(AgentRun)
            .where(*_holding(lease))
            .values(lease_token=None, lease_until=None, updated_at=AgentRun.updated_at)
        )
        db.commit()


def _async_redis() -> aioredis.Redis:
    # One per use: a client belongs to the event loop it was made on.
    return aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)


async def _publish(channel: str) -> None:
    try:
        async with _async_redis() as client:
            await client.publish(channel, "1")
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't publish %s: %s", channel, exc)


@asynccontextmanager
async def _subscribed(channel: str) -> AsyncIterator[aioredis.client.PubSub | None]:
    """A subscription to the channel; None if Redis can't be reached (callers poll instead)."""
    client = _async_redis()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    try:
        try:
            await pubsub.subscribe(channel)
        except (redis.RedisError, OSError) as exc:
            log.warning("couldn't subscribe to %s: %s", channel, exc)
            yield None
        else:
            yield pubsub
    finally:
        with CancelScope(shield=True), suppress(redis.RedisError, OSError):
            await pubsub.aclose()
            await client.aclose()


async def _next_message(pubsub: aioredis.client.PubSub | None, timeout: float) -> bool:
    """Wait up to `timeout` seconds for a message on the subscription; whether one came."""
    if pubsub is None:
        await asyncio.sleep(timeout)
        return False
    try:
        return await pubsub.get_message(timeout=timeout) is not None
    except (redis.RedisError, OSError):
        await asyncio.sleep(timeout)
        return False


# The fallback poll, while waiting for a release that may not be announced.
POLL_SECONDS = 1.0


async def acquire(data_store_id: uuid.UUID, run_id: int, wait: float | None = None) -> Lease:
    """The run's lease, waiting up to `wait` seconds (`turn_wait_seconds`) for the turn that
    holds it to release it. Raises `RunBusy` past that, and `RunGone` if the run is deleted."""
    if wait is None:
        wait = get_settings().turn_wait_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    lease = await asyncio.to_thread(try_acquire, data_store_id, run_id)
    if lease is not None:
        return lease
    # Subscribed before trying again, so a release between the two isn't missed.
    async with _subscribed(released_channel(data_store_id, run_id)) as pubsub:
        while True:
            lease = await asyncio.to_thread(try_acquire, data_store_id, run_id)
            if lease is not None:
                return lease
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RunBusy
            await _next_message(pubsub, min(POLL_SECONDS, remaining))


async def release(lease: Lease) -> None:
    """Let go of the run (if the lease is still this turn's), and tell any turn waiting."""
    await asyncio.to_thread(_clear, lease)
    await _publish(released_channel(lease.data_store_id, lease.run_id))


async def _heartbeat(lease: Lease) -> None:
    interval = get_settings().turn_lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        try:
            still = await asyncio.to_thread(refresh, lease)
        except Exception:
            # The next beat tries again; the lease lasts three of them.
            log.exception("couldn't refresh the turn lease of run %s", lease.run_id)
            continue
        if not still:
            log.warning("run %s: its turn lost its lease", lease.run_id)
            lease.stop()
            return


async def _listen_for_stop(lease: Lease, subscribed: asyncio.Event) -> None:
    async with _subscribed(stop_channel(lease.data_store_id, lease.run_id)) as pubsub:
        subscribed.set()
        if pubsub is None:
            return
        # Stopped before this subscribed: the token's already gone.
        if not await asyncio.to_thread(holds, lease):
            lease.stop()
            return
        while True:
            try:
                message = await pubsub.get_message(timeout=None)
            except (redis.RedisError, OSError) as exc:
                log.warning("run %s: stopped listening for a stop signal: %s", lease.run_id, exc)
                return
            if message is not None:
                lease.stop()
                return


@asynccontextmanager
async def held(lease: Lease) -> AsyncIterator[Lease]:
    """While a turn holds the run: its heartbeat refreshes the lease, and a stop signal for the
    run stops it (`Lease.stop`, which by default cancels the task that entered this). Released
    on exit, however the turn ends."""
    if lease.on_stop is None:
        task = asyncio.current_task()
        if task is not None:
            lease.on_stop = task.cancel
    subscribed = asyncio.Event()
    tasks = [
        asyncio.create_task(_heartbeat(lease)),
        asyncio.create_task(_listen_for_stop(lease, subscribed)),
    ]
    # Listening before the turn starts, so a stop right after is heard.
    with suppress(TimeoutError):
        await asyncio.wait_for(subscribed.wait(), timeout=5)
    try:
        yield lease
    finally:
        with CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await release(lease)
            except Exception:
                # It lapses on its own.
                log.exception("couldn't release the turn lease of run %s", lease.run_id)


def stop(db: Session, data_store_id: uuid.UUID, run_ids: list[int]) -> None:
    """Stop the turns underway of these runs of the store `data_store_id` (`db`, a session on
    it), wherever they run: their leases are cleared, so each turn's next write fails, and a stop
    signal tells the process holding each to cancel it (a model or tool call already underway
    ends, unrecorded). No pooled connection is ended. Commits."""
    if not run_ids:
        return
    db.execute(
        update(AgentRun)
        .where(AgentRun.id.in_(run_ids), AgentRun.lease_token.is_not(None))
        .values(lease_token=None, lease_until=None, updated_at=AgentRun.updated_at)
    )
    db.commit()
    try:
        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        with client:
            for run_id in run_ids:
                client.publish(stop_channel(data_store_id, run_id), "1")
    except (redis.RedisError, OSError) as exc:
        # Their next write fails on fencing regardless.
        log.warning("couldn't signal runs %s to stop: %s", run_ids, exc)


def underway(db: Session, run_ids: list[int]) -> set[int]:
    """Those of these runs (of the store `db` is a session on) with a turn underway, in any
    process: an unexpired lease."""
    if not run_ids:
        return set()
    return set(
        db.scalars(
            select(AgentRun.id).where(
                AgentRun.id.in_(run_ids),
                AgentRun.lease_token.is_not(None),
                AgentRun.lease_until > func.now(),
            )
        )
    )
