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
  while a fire runs, the process refreshes its run's heartbeat (`agent_runs.heartbeat_at`, in
  the run's store) and the cause's own (in the same store) every `HEARTBEAT_SECONDS` (`beat`).
  Each cause sweeps its own rows whose heartbeat has stopped for `STALE_AFTER`, and fails their
  runs' turns too if still running with a stale heartbeat (`fail_stalled`): runs are in each
  organisation's store, and a cause names its run.
- Stopping: a fire in flight when the process stops is cancelled, which stops its turn like a chat
  turn the client abandons (what the agent got to is kept) and records that it was `STOPPED`.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple, Protocol

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from aios import invocations, turns
from aios.api import agents as agents_api
from aios.config import get_settings
from aios.datastores import DataStoreBusy, DataStoreNotFound, DataStoreNotReady, store_session
from aios.datastores.models import AgentRun
from aios.db import SessionLocal

log = logging.getLogger(__name__)

# How often a process refreshes the heartbeats of its fires, and how long without one until a
# running fire counts as stopped: a few missed beats, so a slow write or a busy loop doesn't fail
# a live fire. (As `aios.workflows` does for workflow runs.)
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# Why a fire is failed when its process stops, or is found to have stopped.
STOPPED = "The service stopped mid-run."


class Turn(NamedTuple):
    """What a cause's run starts with: the run (in the store `data_store_id`), the user's
    message, and what to title the run after if not that (a chat mention's request, rather than
    its brief). Whoever started the run, it executes for its agent's organisation
    (`aios.api.agents.load_chat_context`)."""

    data_store_id: uuid.UUID
    agent_id: int
    run_id: int
    message: str
    title: str | None = None


class CantStart(Exception):
    """The run couldn't start; the message is recorded as the outcome."""


class Cause(Protocol):
    """What a caller supplies to `fire`: the parts of a firing that are the cause's own."""

    async def prepare(self) -> None:
        """Whatever the cause does before its run starts, in the fire's slot (a chat mention's
        handoff, `aios.handoff`). Raises `CantStart` to record why the run can't start."""

    def start(self) -> Turn | None:
        """Start the run and commit it, checking whatever the cause claimed is still its to
        run: the turn to run here, or None if there's nothing to run in this process (the
        cause started its run elsewhere), which is recorded as having gone fine.

        Raises `CantStart` to record why it couldn't, or `HTTPException` as starting a run by
        hand would, whose detail is recorded."""

    def beat(self) -> None:
        """Refresh the cause's own heartbeat, if it keeps one, and commit it."""

    def record(self, error: str | None) -> None:
        """Record how the fire went (None if it succeeded), and commit it."""


@dataclass
class Fire:
    """One cause being fired: in flight from `launch` until its task is done."""

    cause: Cause
    # The run it started (its store and id there), once it has: beaten while the fire runs.
    run: tuple[uuid.UUID, int] | None = None


# Fires in flight: kept so their tasks aren't garbage-collected, can be stopped on shutdown
# (`stop_all`), count against the cap (`free_slots`), and have their heartbeats refreshed.
_in_flight: dict[asyncio.Task, Fire] = {}


def free_slots() -> int:
    """How many more fires this process may launch."""
    in_flight = sum(not task.done() for task in _in_flight)
    return get_settings().scheduler_max_concurrent_runs - in_flight


def fail_stalled_in(store: Session, run_ids: list[int]) -> None:
    """Fail the running turns of those of these runs (of the store `store` is a session on)
    whose heartbeat has stopped, and the invocations no chat follows of them: the process firing
    them has gone (see the module docstring); for a cause sweeping its own rows. A run with no
    heartbeat is a request's, left to it (a null heartbeat is never stale). In the caller's
    transaction."""
    if not run_ids:
        return
    failed = turns.fail_running(
        store,
        AgentRun.id.in_(run_ids),
        AgentRun.heartbeat_at < func.now() - STALE_AFTER,
        error=STOPPED,
    )
    if failed:
        store.execute(update(AgentRun).where(AgentRun.id.in_(failed)).values(updated_at=func.now()))
    for run_id in failed:
        invocations.finish_quietly(store, run_id, STOPPED)


def fail_stalled(data_store_id: uuid.UUID, run_ids: list[int]) -> None:
    """`fail_stalled_in` the store, committed. A store that can't be reached now is left for
    the next sweep."""
    if not run_ids:
        return
    try:
        with store_session(data_store_id) as store:
            fail_stalled_in(store, run_ids)
            store.commit()
    except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy) as exc:
        log.warning("couldn't fail the stalled runs of data store %s: %s", data_store_id, exc)


def _beat(flights: list[Fire]) -> None:
    runs: dict[uuid.UUID, list[int]] = {}
    for flight in flights:
        if flight.run is not None:
            runs.setdefault(flight.run[0], []).append(flight.run[1])
    for data_store_id, run_ids in runs.items():
        with store_session(data_store_id) as store:
            # Not once its turn has ended, nor one a chat turn has since taken over.
            store.execute(
                update(AgentRun)
                .where(
                    AgentRun.id.in_(run_ids),
                    turns.running(AgentRun.id),
                    AgentRun.heartbeat_at.is_not(None),
                )
                .values(heartbeat_at=func.now())
            )
            store.commit()
    for flight in flights:
        flight.cause.beat()


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


def _start(flight: Fire) -> tuple[Turn, agents_api.ChatContext, int | None] | None:
    """Start the cause's run, and the turn: begun, with its run's heartbeat; and its id."""
    turn = flight.cause.start()
    if turn is None:
        return None
    with SessionLocal() as db:
        ctx = agents_api.load_chat_context(db, turn.data_store_id, turn.agent_id, turn.run_id)
    turn_id = agents_api._start_turn(
        turn.data_store_id, turn.run_id, turn.title or turn.message, heartbeat=True
    )
    flight.run = (turn.data_store_id, turn.run_id)
    return turn, ctx, turn_id


def _record(cause: Cause, error: str | None) -> None:
    cause.record(error)


async def _fire(flight: Fire) -> None:
    error = None
    try:
        await flight.cause.prepare()
        started = await run_in_threadpool(_start, flight)
        if started is not None:
            turn, ctx, turn_id = started
            stream = agents_api.turn_stream(
                ctx, turn.run_id, HumanMessage(content=turn.message), turn_id=turn_id
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
