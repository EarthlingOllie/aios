"""Fires schedule triggers (`aios.models.Trigger`).

Each API process runs `run_forever` alongside serving requests. Every tick it claims the
schedules that are due (`claim_due`) and fires each in a task of its own (`fire`):

- A process fires at most `scheduler_max_concurrent_runs` at once, and claims no more than it has
  free slots for: claiming moves a schedule on, so one claimed but left waiting would be late or,
  if the process stopped, lost. What it leaves stays due, for another replica or a later tick.

- Claiming locks the due rows with SKIP LOCKED and moves each schedule's `next_run_at` on in the
  same transaction, so however many replicas poll, each run of a schedule is claimed once.
- Missed runs aren't caught up: a schedule that's overdue (no process was up) fires once, and its
  next run is worked out from now.
- Firing an agent's schedule starts a new run of the agent's current version, with the trigger's
  `input` as the user's message, and runs one turn exactly as the chat endpoint does
  (`aios.api.agents.turn_stream`). The outcome goes on the trigger (`last_status`, `last_error`,
  `last_run_id`), unless the schedule has fired again since.
- Firing a workflow's schedule starts a run of the workflow's current version, with the
  trigger's `input` as the run's input, exactly as `POST /api/workflows/{id}/runs` does
  (`aios.api.workflows.new_workflow_run`), and queues it for the worker. The trigger records the
  run (`last_workflow_run_id`) and, once it's queued, that it fired; from then on it shows how the
  run is going (`aios.api.triggers.last_outcome`). One that can't start (no nodes, an agent
  that can't run, the queue down) records that it failed, without a run.
- Editing a schedule (pausing it, changing its target or message, ...) after it's claimed but
  before its run has started drops that firing: it was for the old settings
  (`aios.api.triggers.cancel_unstarted_firing`). Once the run has started, it finishes.
- A turn in flight when the process stops is stopped like a chat turn the client abandons: what
  the agent got to is kept, and the schedule records that it failed.
- A process that dies (killed, OOM, a deploy) can't record that, nor can a tick stopped between
  claiming and firing. So while a schedule is stored as running, the process firing it refreshes
  its heartbeat (`heartbeat_at`, set when it's claimed) every `HEARTBEAT_SECONDS` (`beat`), and
  each tick, on any replica, fails those whose heartbeat has stopped for `STALE_AFTER`
  (`fail_stalled`). A live replica keeps beating its own fires, so others leave them be. Only the
  stored status counts: a workflow's schedule is stored as running only until its run is queued,
  and the worker looks after the run from there (`aios.workflows`).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import func, or_, select, tuple_, update

from aios import cron
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import Trigger, TriggerStatus, TriggerTargetKind

log = logging.getLogger(__name__)

# Most schedules claimed per tick, however many slots are free; any left over are due on the next.
BATCH = 50

# How often a process refreshes the heartbeat of the schedules it's firing, and how long without
# one until a running schedule counts as stopped: a few missed beats, so a slow write or a busy
# loop doesn't fail a live fire. (As `aios.workflows` does for workflow runs.)
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# Why a fire is failed when its process stops, or is found to have stopped.
STOPPED = "The service stopped mid-run."
# Why one is failed if it was dropped (see `_claimed`) without its schedule being edited.
CHANGED = "The schedule was changed as it fired; it runs as changed next time."

# Fires in flight, with the (trigger id, fired at) of each: kept so they aren't
# garbage-collected, can be stopped on shutdown, and have their heartbeats refreshed.
_firing: dict[asyncio.Task, tuple[int, datetime]] = {}


class FireError(Exception):
    """The trigger couldn't fire; the message is shown on it."""


def claim_due(db, now: datetime, limit: int = BATCH) -> list[int]:
    """Claim up to `limit` of the schedules due at `now`, the most overdue first: move each on to
    its next run, and return their ids."""
    due = db.scalars(
        select(Trigger)
        .where(Trigger.next_run_at <= now)
        .order_by(Trigger.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed = []
    for trigger in due:
        trigger.last_fired_at = now
        # This firing's run, once it has one; until then the trigger shows it running.
        trigger.last_run_id = None
        trigger.last_workflow_run_id = None
        try:
            trigger.next_run_at = cron.next_run(trigger.cron, trigger.timezone, now)
        except cron.CronError as exc:
            # Valid when saved, so only if, say, its timezone has left the tz database.
            trigger.enabled = False
            trigger.next_run_at = None
            trigger.last_status = TriggerStatus.failed
            trigger.last_error = f"{exc}. The schedule has been turned off."
            continue
        trigger.last_status = TriggerStatus.running
        trigger.last_error = None
        trigger.heartbeat_at = func.now()
        claimed.append(trigger.id)
    db.commit()
    return claimed


def fail_stalled(db) -> None:
    """Fail the schedules stored as running whose heartbeat has stopped: the process firing
    them has gone (see the module docstring). Commits."""
    db.execute(
        update(Trigger)
        .where(
            Trigger.last_status == TriggerStatus.running,
            or_(Trigger.heartbeat_at.is_(None), Trigger.heartbeat_at < func.now() - STALE_AFTER),
        )
        .values(last_status=TriggerStatus.failed, last_error=STOPPED)
    )
    db.commit()


def _sweep() -> None:
    with SessionLocal() as db:
        fail_stalled(db)


def _claim(now: datetime, limit: int) -> list[int]:
    with SessionLocal() as db:
        return claim_due(db, now, limit)


def _beat(fires: list[tuple[int, datetime]]) -> None:
    with SessionLocal() as db:
        # Not once it's recorded how it went, or fired again (on another replica, say).
        db.execute(
            update(Trigger)
            .where(
                tuple_(Trigger.id, Trigger.last_fired_at).in_(fires),
                Trigger.last_status == TriggerStatus.running,
            )
            .values(heartbeat_at=func.now())
        )
        db.commit()


async def beat() -> None:
    """Refresh the heartbeat of the schedules this process is firing."""
    if fires := list(_firing.values()):
        await run_in_threadpool(_beat, fires)


async def _beat_forever() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await beat()
        except Exception:
            # Only several missed beats in a row count as stopped.
            log.exception("scheduler heartbeat failed")


# What an agent's schedule runs once its run has started: (user id, agent id, run id, message).
AgentTurn = tuple[int, int, int, str]


class _Gone(Exception):
    """The trigger has gone, fired again, or changed target since it was claimed."""


def _target(trigger_id: int, fired_at: datetime) -> TriggerTargetKind | None:
    """What the claimed trigger targets; None if it has gone or fired again since."""
    with SessionLocal() as db:
        trigger = db.get(Trigger, trigger_id)
        if trigger is None or trigger.last_fired_at != fired_at:
            return None
        return trigger.target_kind


def _claimed(
    db, trigger_id: int, fired_at: datetime, kind: TriggerTargetKind, *, lock: bool = False
) -> Trigger:
    """The trigger, still claimed by this firing; else `_Gone`.

    Editing a trigger (pausing it, say) drops a firing that hasn't started its run
    (`aios.api.triggers.cancel_unstarted_firing`), which this sees as it having fired again.
    With `lock`, the row stays locked until commit, so the run and the trigger's link to it are
    committed only if the firing is still claimed, and an edit racing the start waits and then
    sees the run. Take it after the target's lock (`new_agent_run`, `new_workflow_run`): a
    delete or an edit locks the target first, then the trigger.
    """
    query = select(Trigger).where(Trigger.id == trigger_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    trigger = db.scalars(query).one_or_none()
    # Changed target: edited since it was claimed, so it fires the new one next time.
    if trigger is None or trigger.last_fired_at != fired_at or trigger.target_kind != kind:
        raise _Gone
    return trigger


def _start_agent_run(trigger_id: int, fired_at: datetime) -> AgentTurn:
    """Start a run for an agent's schedule."""
    with SessionLocal() as db:
        trigger = _claimed(db, trigger_id, fired_at, TriggerTargetKind.agent)
        agent = trigger.agent
        try:
            run = agents_api.new_agent_run(db, agent, trigger_id=trigger.id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise FireError("The agent has been deleted.") from exc
            raise FireError(exc.detail) from exc
        trigger = _claimed(db, trigger_id, fired_at, TriggerTargetKind.agent, lock=True)
        # Committed with the run, under the agent's share lock (see `new_agent_run`).
        trigger.last_run_id = run.id
        db.commit()
        return agent.owner_id, agent.id, run.id, trigger.input


def _start_workflow_run(trigger_id: int, fired_at: datetime) -> None:
    """Start a run for a workflow's schedule, and queue it for the worker (`aios.workflows`).

    Raises `HTTPException` as starting one by hand would, if it can't start or be queued."""
    with SessionLocal() as db:
        trigger = _claimed(db, trigger_id, fired_at, TriggerTargetKind.workflow)
        run = workflows_api.new_workflow_run(db, trigger.workflow, trigger.input)
        trigger = _claimed(db, trigger_id, fired_at, TriggerTargetKind.workflow, lock=True)
        trigger.last_workflow_run_id = run.id
        # Commits the link with the run; if queueing fails the run is deleted, and the link
        # with it (SET NULL).
        workflows_api.queue_workflow_run(db, run)


async def _run_agent(trigger_id: int, fired_at: datetime) -> str | None:
    """Start an agent's run and run its turn; the turn's error, if it failed."""
    user_id, agent_id, run_id, message = await run_in_threadpool(
        _start_agent_run, trigger_id, fired_at
    )
    ctx = await run_in_threadpool(_load_context, user_id, agent_id, run_id)
    await run_in_threadpool(agents_api._start_turn, run_id, message)
    error = None
    async for chunk in agents_api.turn_stream(ctx, user_id, run_id, HumanMessage(content=message)):
        if chunk["type"] == "error":
            error = chunk["errorText"]
    return error


def _record(trigger_id: int, fired_at: datetime, error: str | None) -> None:
    with SessionLocal() as db:
        # A no-op if the trigger was deleted, or has fired again since.
        db.execute(
            update(Trigger)
            .where(Trigger.id == trigger_id, Trigger.last_fired_at == fired_at)
            .values(
                last_status=TriggerStatus.failed if error else TriggerStatus.succeeded,
                last_error=error,
            )
        )
        db.commit()


def _load_context(user_id: int, agent_id: int, run_id: int) -> agents_api.ChatContext:
    with SessionLocal() as db:
        return agents_api.load_chat_context(db, user_id, agent_id, run_id)


async def fire(trigger_id: int, fired_at: datetime) -> None:
    """Fire one claimed trigger, and record how it went."""
    error = None
    try:
        kind = await run_in_threadpool(_target, trigger_id, fired_at)
        if kind is None:
            return
        if kind == TriggerTargetKind.workflow:
            # Queued for the worker: the trigger shows how the run goes from here.
            await run_in_threadpool(_start_workflow_run, trigger_id, fired_at)
        else:
            error = await _run_agent(trigger_id, fired_at)
    except _Gone:
        # A no-op: it's gone, fired again, or was edited, which drops the firing (see
        # `_claimed`). Recorded anyway, so a firing dropped some other way isn't left running.
        error = CHANGED
    except FireError as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            await run_in_threadpool(_record, trigger_id, fired_at, STOPPED)
        raise
    except Exception:
        log.exception("trigger %s couldn't fire", trigger_id)
        error = "Couldn't start the run."
    await run_in_threadpool(_record, trigger_id, fired_at, error)


def _forget(task: asyncio.Task) -> None:
    _firing.pop(task, None)


async def tick(now: datetime | None = None) -> dict[int, asyncio.Task]:
    """Claim what's due, as much as there are free slots for, and fire each in a task of its own;
    returns the tasks by trigger id."""
    now = now or datetime.now(UTC)
    # Even with no free slot: with one replica, nothing else would fail what a dead process left.
    await run_in_threadpool(_sweep)
    # Every fire counts, a workflow's too: it holds its slot only while it starts and queues
    # the run, and counting it keeps the bound simple and honest.
    in_flight = sum(not task.done() for task in _firing)
    free = min(BATCH, get_settings().scheduler_max_concurrent_runs - in_flight)
    fired = {}
    if free <= 0:
        return fired
    for trigger_id in await run_in_threadpool(_claim, now, free):
        task = asyncio.create_task(fire(trigger_id, now), name=f"trigger-{trigger_id}")
        _firing[task] = (trigger_id, now)
        task.add_done_callback(_forget)
        fired[trigger_id] = task
    return fired


async def run_forever(interval: float) -> None:
    """Tick every `interval` seconds, and beat the fires in flight every `HEARTBEAT_SECONDS`,
    until cancelled; then stop the fires still running."""
    beating = asyncio.create_task(_beat_forever(), name="scheduler-heartbeat")
    try:
        while True:
            try:
                await tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(interval)
    finally:
        beating.cancel()
        for task in list(_firing):
            task.cancel()
        await asyncio.gather(beating, *_firing, return_exceptions=True)
