"""Fires agent runs for the causes that start them outside a request.

A schedule firing (`aios.scheduler`) and, later, a chat mention are one operation with different
causes: start a run of the agent, run one turn of it in this process, exactly as the chat endpoint
does (`aios.api.agents.turn_stream`), and record how it went wherever the cause keeps its outcome.
`fire` is that operation. What's the cause's own is a `Cause`: what it claims and how it starts
the run, where its heartbeat lives, and where the outcome goes.

Running turns in a process that also serves requests needs:

- A cap: a process runs at most `scheduler_max_concurrent_runs` fires at once (`free_slots`), and
  a caller starts no more than there are free slots for (`launch` counts each until its task is
  done). What it can't start is left for another replica or a later try.
- A heartbeat: a process that dies (killed, OOM, a deploy) can't record how its fires went. So
  while a fire runs, the process refreshes its run's heartbeat (`agent_runs.heartbeat_at`) and
  the cause's own every `HEARTBEAT_SECONDS` (`beat`), and any process fails the runs whose
  heartbeat has stopped for `STALE_AFTER` (`fail_stalled`). A cause that's stored as running
  before its run exists (a claimed schedule) sweeps its own rows too, by the same measure.
- Stopping: a fire in flight when the process stops is cancelled, which stops its turn like a chat
  turn the client abandons (what the agent got to is kept) and records that it was `STOPPED`.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple, Protocol

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from aios.api import agents as agents_api
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import AgentRun, AgentRunStatus

log = logging.getLogger(__name__)

# How often a process refreshes the heartbeats of its fires, and how long without one until a
# running fire counts as stopped: a few missed beats, so a slow write or a busy loop doesn't fail
# a live fire. (As `aios.workflows` does for workflow runs.)
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# Why a fire is failed when its process stops, or is found to have stopped.
STOPPED = "The service stopped mid-run."


class Turn(NamedTuple):
    """What a cause's run starts with: whose it is, and the user's message."""

    user_id: int
    agent_id: int
    run_id: int
    message: str


class CantStart(Exception):
    """The run couldn't start; the message is recorded as the outcome."""


class Cause(Protocol):
    """What a caller supplies to `fire`: the parts of a firing that are the cause's own."""

    async def prepare(self) -> None:
        """Whatever the cause does before its run starts, in the fire's slot (a chat mention's
        handoff, `aios.handoff`). Raises `CantStart` to record why the run can't start."""

    def start(self, db: Session) -> Turn | None:
        """Start the run and commit it, checking whatever the cause claimed is still its to
        run: the turn to run here, or None if there's nothing to run in this process (the
        cause started its run elsewhere), which is recorded as having gone fine.

        Raises `CantStart` to record why it couldn't, or `HTTPException` as starting a run by
        hand would, whose detail is recorded."""

    def beat(self, db: Session) -> None:
        """Refresh the cause's own heartbeat, if it keeps one. Committed by the caller."""

    def record(self, db: Session, error: str | None) -> None:
        """Record how the fire went: None if it succeeded. Committed by the caller."""


@dataclass
class Fire:
    """One cause being fired: in flight from `launch` until its task is done."""

    cause: Cause
    # The run it started, once it has: beaten while the fire runs.
    run_id: int | None = None


# Fires in flight: kept so their tasks aren't garbage-collected, can be stopped on shutdown
# (`stop_all`), count against the cap (`free_slots`), and have their heartbeats refreshed.
_in_flight: dict[asyncio.Task, Fire] = {}


def free_slots() -> int:
    """How many more fires this process may launch."""
    in_flight = sum(not task.done() for task in _in_flight)
    return get_settings().scheduler_max_concurrent_runs - in_flight


def fail_stalled(db: Session) -> None:
    """Fail the runs stored as running whose heartbeat has stopped: the process firing them has
    gone (see the module docstring). A run with no heartbeat is a request's, left to it (a null
    heartbeat is never stale). Commits."""
    db.execute(
        update(AgentRun)
        .where(
            AgentRun.status == AgentRunStatus.running,
            AgentRun.heartbeat_at < func.now() - STALE_AFTER,
        )
        .values(status=AgentRunStatus.failed, finished_at=func.now(), updated_at=func.now())
    )
    db.commit()


def _beat(flights: list[Fire]) -> None:
    with SessionLocal() as db:
        if run_ids := [flight.run_id for flight in flights if flight.run_id is not None]:
            # Not once its turn has ended, nor one a chat turn has since taken over.
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id.in_(run_ids),
                    AgentRun.status == AgentRunStatus.running,
                    AgentRun.heartbeat_at.is_not(None),
                )
                .values(heartbeat_at=func.now())
            )
        for flight in flights:
            flight.cause.beat(db)
        db.commit()


async def beat() -> None:
    """Refresh the heartbeats of the fires this process is running."""
    if flights := list(_in_flight.values()):
        await run_in_threadpool(_beat, flights)


async def beat_forever(interval: float) -> None:
    """Beat every `interval` seconds until cancelled."""
    while True:
        await asyncio.sleep(interval)
        try:
            await beat()
        except Exception:
            # Only several missed beats in a row count as stopped.
            log.exception("heartbeat failed")


def _start(flight: Fire) -> tuple[Turn, agents_api.ChatContext] | None:
    """Start the cause's run, and the turn: its run marked running, with its heartbeat."""
    with SessionLocal() as db:
        turn = flight.cause.start(db)
        if turn is None:
            return None
        ctx = agents_api.load_chat_context(db, turn.user_id, turn.agent_id, turn.run_id)
    agents_api._start_turn(turn.run_id, turn.message, heartbeat=True)
    flight.run_id = turn.run_id
    return turn, ctx


def _record(cause: Cause, error: str | None) -> None:
    with SessionLocal() as db:
        cause.record(db, error)
        db.commit()


async def _fire(flight: Fire) -> None:
    error = None
    try:
        await flight.cause.prepare()
        started = await run_in_threadpool(_start, flight)
        if started is not None:
            turn, ctx = started
            stream = agents_api.turn_stream(
                ctx, turn.user_id, turn.run_id, HumanMessage(content=turn.message)
            )
            async for chunk in stream:
                if chunk["type"] == "error":
                    error = chunk["errorText"]
    except CantStart as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            await run_in_threadpool(_record, flight.cause, STOPPED)
        raise
    except Exception:
        log.exception("%r couldn't fire", flight.cause)
        error = "Couldn't start the run."
    await run_in_threadpool(_record, flight.cause, error)


async def fire(cause: Cause) -> None:
    """Fire one cause: start its run, run one turn, and record how it went."""
    await _fire(Fire(cause))


def _forget(task: asyncio.Task) -> None:
    _in_flight.pop(task, None)


def launch(cause: Cause, *, name: str) -> asyncio.Task:
    """Fire the cause in a task of its own, counted against the cap and beaten while it runs."""
    flight = Fire(cause)
    task = asyncio.create_task(_fire(flight), name=name)
    _in_flight[task] = flight
    task.add_done_callback(_forget)
    return task


async def stop_all() -> None:
    """Stop the fires in flight, as the process stops, and wait for them to record that."""
    for task in list(_in_flight):
        task.cancel()
    await asyncio.gather(*_in_flight, return_exceptions=True)
