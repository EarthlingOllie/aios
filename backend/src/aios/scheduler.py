"""Fires schedule triggers (`aios.models.Trigger`).

Each API process runs `run_forever` alongside serving requests. Every tick it claims the
schedules that are due (`claim_due`) and fires each in a task of its own (`fire`):

- Claiming locks the due rows with SKIP LOCKED and moves each schedule's `next_run_at` on in the
  same transaction, so however many replicas poll, each run of a schedule is claimed once.
- Missed runs aren't caught up: a schedule that's overdue (no process was up) fires once, and its
  next run is worked out from now.
- Firing an agent's schedule starts a new run of the agent's current version, with the trigger's
  `input` as the user's message, and runs one turn exactly as the chat endpoint does
  (`aios.api.agents.turn_stream`). The outcome goes on the trigger (`last_status`, `last_error`,
  `last_run_id`), unless the schedule has fired again since.
- Workflows can't run yet, so firing a workflow's schedule records that it failed.
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
from aios.api.skills import lock_live
from aios.db import SessionLocal
from aios.models import Agent, AgentRun, Trigger, TriggerStatus, TriggerTargetKind

log = logging.getLogger(__name__)

# Schedules claimed per tick; any left over are due on the next one.
BATCH = 50

# Fires in flight, kept so they aren't garbage-collected and can be stopped on shutdown.
_firing: set[asyncio.Task] = set()


class FireError(Exception):
    """The trigger couldn't fire; the message is shown on it."""


def claim_due(db, now: datetime) -> list[int]:
    """Claim the schedules due at `now`: move each on to its next run, and return their ids."""
    due = db.scalars(
        select(Trigger)
        .where(Trigger.next_run_at <= now)
        .order_by(Trigger.next_run_at)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    claimed = []
    for trigger in due:
        trigger.last_fired_at = now
        trigger.last_run_id = None
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


def _claim(now: datetime) -> list[int]:
    with SessionLocal() as db:
        return claim_due(db, now)


def _start_agent_run(trigger_id: int, fired_at: datetime) -> tuple[int, int, int, str] | None:
    """Start a run for an agent's schedule: (user id, agent id, run id, message). None if the
    trigger has gone or fired again since."""
    with SessionLocal() as db:
        trigger = db.get(Trigger, trigger_id)
        if trigger is None or trigger.last_fired_at != fired_at:
            return None
        if trigger.target_kind == TriggerTargetKind.workflow:
            raise FireError("Workflows can't be run yet.")
        agent = trigger.agent
        # Keeps a concurrent delete of the agent from missing this run (see `create_run`).
        if not lock_live(db, Agent, [agent.id], read=True):
            raise FireError("The agent has been deleted.")
        try:
            agents_api.build_version_tools(agent.current_version)
        except HTTPException as exc:
            raise FireError(exc.detail) from exc
        run = AgentRun(
            agent=agent, agent_version_id=agent.current_version_id, trigger_id=trigger.id
        )
        db.add(run)
        db.flush()
        trigger.last_run_id = run.id
        db.commit()
        return agent.owner_id, agent.id, run.id, trigger.input


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
        started = await run_in_threadpool(_start_agent_run, trigger_id, fired_at)
        if started is None:
            return
        user_id, agent_id, run_id, message = started
        ctx = await run_in_threadpool(_load_context, user_id, agent_id, run_id)
        await run_in_threadpool(agents_api._start_turn, run_id, message)
        async for chunk in agents_api.turn_stream(
            ctx, user_id, run_id, HumanMessage(content=message)
        ):
            if chunk["type"] == "error":
                error = chunk["errorText"]
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
    """Claim what's due and fire each in a task of its own; returns the tasks by trigger id."""
    now = now or datetime.now(UTC)
    fired = {}
    for trigger_id in await run_in_threadpool(_claim, now):
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
