"""The runs a chat's mentions start: a workflow's run, or an agent's turn under the firing
machinery's cap (`aios.firing`). An invocation is named by its store and its id there."""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import firing, handoff, invocations
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.config import get_settings
from aios.datastores import ready_stores, store_session
from aios.datastores.models import Agent, ChatMessage, Invocation, Workflow
from aios.db import SessionLocal
from aios.models import DataStore, InvocationStatus

log = logging.getLogger(__name__)


def _organisation(data_store_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
    if organisation_id is None:
        raise firing.CantStart("The organisation has been deleted.")
    return organisation_id


@dataclass(frozen=True)
class InvocationFiring:
    """Starting an invocation's run, as a cause of a run (`aios.firing.Cause`)."""

    data_store_id: uuid.UUID
    invocation_id: int

    async def prepare(self) -> None:
        await handoff.brief(self.data_store_id, self.invocation_id)

    def start(self) -> None:
        """Start the workflow's run with the brief and the message as its input and queue it for
        the worker; the run then finishes the invocation (`aios.invocations`). Raises
        `HTTPException` as starting one by hand would, or `CantStart`."""
        organisation_id = _organisation(self.data_store_id)
        with store_session(self.data_store_id) as store:
            invocation = store.get(Invocation, self.invocation_id)
            if invocation is None or invocation.status != InvocationStatus.briefing:
                return None
            message = store.get_one(ChatMessage, invocation.message_id)
            workflow = store.get(Workflow, invocation.workflow_id)
            if workflow is None:
                raise firing.CantStart("The workflow has been deleted.")
            # The workflow's share lock first, then the invocation's.
            run = workflows_api.new_workflow_run(
                store,
                organisation_id,
                workflow,
                invocations.run_input(invocation.brief, message.body),
                created_by=invocation.requested_by,
                by_mention=True,
            )
            invocation = invocations.lock(store, self.invocation_id)
            if invocation is None or invocation.status != InvocationStatus.briefing:
                store.rollback()
                return None
            invocation.workflow_run_id = run.id
            # Commits the run with the invocation's link to it, and queues it; if it can't be
            # queued the run is deleted, and the link with it.
            workflows_api.queue_workflow_run(store, run)
            invocations.mark_running(store, self.invocation_id)
            store.commit()
        return None

    def beat(self) -> None:
        # Nothing of its own to keep alive: it only starts a run, which the worker looks after.
        pass

    def record(self, error: str | None) -> None:
        if error is not None:
            with store_session(self.data_store_id) as store:
                invocations.fail_unstarted(store, self.invocation_id, error)
                store.commit()


@dataclass(frozen=True)
class TurnFiring:
    """Running an agent invocation's turn in this process, which has claimed it
    (`invocations.claim_turns`), as a cause of a run (`aios.firing.Cause`): capped, beaten while
    it runs, and failed if its heartbeat stops (`fail_stopped_turns`)."""

    data_store_id: uuid.UUID
    invocation_id: int

    async def prepare(self) -> None:
        await handoff.brief(self.data_store_id, self.invocation_id)

    def start(self) -> firing.Turn | None:
        """Start a run of the agent's current version, started by whoever mentioned it, with the
        brief and the message as the user's; the invocation records it and goes running. None
        if it has finished since it was claimed. Raises `HTTPException` as starting one by hand
        would, or `CantStart`."""
        organisation_id = _organisation(self.data_store_id)
        with store_session(self.data_store_id) as store:
            invocation = store.get(Invocation, self.invocation_id)
            if invocation is None or invocation.status != InvocationStatus.briefing:
                return None
            body = store.get_one(ChatMessage, invocation.message_id).body
            prompt = invocations.run_input(invocation.brief, body)
            agent = store.get(Agent, invocation.agent_id)
            if agent is None:
                raise firing.CantStart("The agent has been deleted.")
            # The agent's share lock first, then the invocation's.
            run = agents_api.new_agent_run(
                store, organisation_id, agent, created_by=invocation.requested_by, by_mention=True
            )
            invocation = invocations.lock(store, self.invocation_id)
            if invocation is None or invocation.status != InvocationStatus.briefing:
                store.rollback()
                return None
            # The run and the invocation's link to it, together.
            invocation.agent_run_id = run.id
            invocations.mark_running(store, self.invocation_id)
            store.commit()
            agent_id, run_id = agent.id, run.id
        # Titled after what was asked.
        return firing.Turn(
            self.data_store_id, agent_id, run_id, prompt, title=invocations.message_text(body)
        )

    def beat(self) -> None:
        with store_session(self.data_store_id) as store:
            invocations.beat(store, self.invocation_id)
            store.commit()

    def record(self, error: str | None) -> None:
        with store_session(self.data_store_id) as store:
            invocations.finish_turn(store, self.invocation_id, error)
            store.commit()


def fail_stopped_turns(store: Session, chat_id: int | None = None) -> None:
    """Fail the store's agent invocations (the chat's, or all) whose turn's process has gone:
    their heartbeat stopped (`aios.firing`). Posts why, and fails their runs. Commits."""
    invocations.fail_stopped_turns(
        store, stale_after=firing.STALE_AFTER, error=firing.STOPPED, chat_id=chat_id
    )
    store.commit()


# An agent invocation claimed for a turn: its store, and its id there.
Claimed = tuple[uuid.UUID, int]


def _claim_turns(limit: int, data_store_id: uuid.UUID | None = None) -> list[Claimed]:
    """Claim up to `limit` agent invocations waiting for a turn: the store's, or every ready
    store's in turn. A store that can't be reached is left for the next try."""
    stores = [data_store_id] if data_store_id is not None else [s for s, _ in ready_stores()]
    claimed: list[Claimed] = []
    for store_id in stores:
        if len(claimed) >= limit:
            break
        try:
            with store_session(store_id) as store:
                ids = invocations.claim_turns(store, limit - len(claimed))
        except Exception:
            log.exception("couldn't claim the turns waiting in data store %s", store_id)
            continue
        claimed.extend((store_id, i) for i in ids)
    return claimed


async def launch_turns(data_store_id: uuid.UUID | None = None) -> list[asyncio.Task]:
    """Claim the agent invocations waiting for a turn (the store's, or any store's), as many as
    this process has free slots for under the cap, and run each in a fire of its own
    (`aios.firing.launch`). Those left wait for a slot here or on another replica: the
    scheduler's tick calls this too. None in a process kept from running agents
    (`scheduler_enabled`), which beats no fires."""
    free = firing.free_slots() if get_settings().scheduler_enabled else 0
    if free <= 0:
        return []
    claimed = await run_in_threadpool(_claim_turns, free, data_store_id)
    return [
        firing.launch(TurnFiring(store_id, i), name=f"invocation-{store_id}-{i}")
        for store_id, i in claimed
    ]


async def start_invocations(data_store_id: uuid.UUID, invocation_ids: list[int]) -> None:
    """Start the store's workflow invocations' runs, each as a fire of its own (`aios.firing`),
    at once. Starting one only briefs it (one model call) and queues its run, so these don't
    wait for a free slot under the cap."""
    await asyncio.gather(
        *(
            firing.launch(
                InvocationFiring(data_store_id, i), name=f"invocation-{data_store_id}-{i}"
            )
            for i in invocation_ids
        )
    )


async def run_turns(data_store_id: uuid.UUID | None = None) -> None:
    """Run the agent invocations waiting for a turn (the store's, or any store's), as slots free
    up under the cap, until none is left waiting or every slot is taken (the scheduler's ticks
    take it from there)."""
    while fires := await launch_turns(data_store_id):
        await asyncio.gather(*fires)
