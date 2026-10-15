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
- A turn in flight when the process stops is stopped like a chat turn the client abandons: what
  the agent got to is kept, and the schedule records that it failed.
"""

import asyncio
import logging
from datetime import UTC, datetime

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import select, update

from aios import cron
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import Trigger, TriggerStatus, TriggerTargetKind

log = logging.getLogger(__name__)

# Most schedules claimed per tick, however many slots are free; any left over are due on the next.
BATCH = 50

# Fires in flight, kept so they aren't garbage-collected and can be stopped on shutdown.
_firing: set[asyncio.Task] = set()


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
        claimed.append(trigger.id)
    db.commit()
    return claimed


def _claim(now: datetime, limit: int) -> list[int]:
    with SessionLocal() as db:
        return claim_due(db, now, limit)


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


def _claimed(db, trigger_id: int, fired_at: datetime, kind: TriggerTargetKind) -> Trigger:
    trigger = db.get(Trigger, trigger_id)
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
        return
    except FireError as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            await run_in_threadpool(_record, trigger_id, fired_at, "The service stopped mid-run.")
        raise
    except Exception:
        log.exception("trigger %s couldn't fire", trigger_id)
        error = "Couldn't start the run."
    await run_in_threadpool(_record, trigger_id, fired_at, error)


async def tick(now: datetime | None = None) -> dict[int, asyncio.Task]:
    """Claim what's due, as much as there are free slots for, and fire each in a task of its own;
    returns the tasks by trigger id."""
    now = now or datetime.now(UTC)
    # Every fire counts, a workflow's too: it holds its slot only while it starts and queues
    # the run, and counting it keeps the bound simple and honest.
    in_flight = sum(not task.done() for task in _firing)
    free = min(BATCH, get_settings().scheduler_max_concurrent_runs - in_flight)
    fired = {}
    if free <= 0:
        return fired
    for trigger_id in await run_in_threadpool(_claim, now, free):
        task = asyncio.create_task(fire(trigger_id, now), name=f"trigger-{trigger_id}")
        _firing.add(task)
        task.add_done_callback(_firing.discard)
        fired[trigger_id] = task
    return fired


async def run_forever(interval: float) -> None:
    """Tick every `interval` seconds until cancelled; then stop the fires still running."""
    try:
        while True:
            try:
                await tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(interval)
    finally:
        for task in list(_firing):
            task.cancel()
        await asyncio.gather(*_firing, return_exceptions=True)
