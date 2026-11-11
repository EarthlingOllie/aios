"""The runs a chat's mentions start: a workflow's run, or an agent's turn under the firing
machinery's cap (`aios.firing`)."""

import asyncio
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from aios import firing, handoff, invocations
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.config import get_settings
from aios.datastores import organisation_store, store_id_of
from aios.datastores.models import Agent, Workflow
from aios.db import SessionLocal
from aios.models import (
    ChatInvocation,
    ChatInvocationStatus,
    ChatMessage,
)


@dataclass(frozen=True)
class InvocationFiring:
    """Starting an invocation's run, as a cause of a run (`aios.firing.Cause`)."""

    invocation_id: int

    async def prepare(self) -> None:
        await handoff.brief(self.invocation_id)

    def start(self, db: Session) -> None:
        """Start the workflow's run with the brief and the message as its input and queue it for
        the worker; the run then finishes the invocation (`aios.invocations`). Raises
        `HTTPException` as starting one by hand would, or `CantStart`."""
        invocation = invocations.lock(db, self.invocation_id)
        if invocation is None or invocation.status != ChatInvocationStatus.briefing:
            return None
        message = db.get(ChatMessage, invocation.message_id)
        organisation_id = invocations.organisation_of(db, invocation)
        with organisation_store(organisation_id) as store:
            workflow = store.get(Workflow, invocation.workflow_id)
            if workflow is None:
                raise firing.CantStart("The workflow has been deleted.")
            run = workflows_api.new_workflow_run(
                store,
                organisation_id,
                workflow,
                invocations.run_input(invocation.brief, message.body),
                created_by=invocation.requested_by,
            )

            def link() -> None:
                invocation.workflow_run_id = run.id
                db.commit()

            # Commits the run in the store (ending the workflow's share lock), then the link to
            # it, and queues it; if it can't be queued the run is deleted, and the link with it.
            workflows_api.queue_workflow_run(store, run, link)
        invocations.mark_running(db, self.invocation_id)
        db.commit()
        return None

    def beat(self, db: Session) -> None:
        # Nothing of its own to keep alive: it only starts a run, which the worker looks after.
        pass

    def record(self, db: Session, error: str | None) -> None:
        if error is not None:
            invocations.fail_unstarted(db, self.invocation_id, error)


@dataclass(frozen=True)
class TurnFiring:
    """Running an agent invocation's turn in this process, which has claimed it
    (`invocations.claim_turns`), as a cause of a run (`aios.firing.Cause`): capped, beaten while
    it runs, and failed if its heartbeat stops (`fail_stopped_turns`)."""

    invocation_id: int

    async def prepare(self) -> None:
        await handoff.brief(self.invocation_id)

    def start(self, db: Session) -> firing.Turn | None:
        """Start a run of the agent's current version, started by whoever mentioned it, with the
        brief and the message as the user's; the invocation records it and goes running. None
        if it has finished since it was claimed. Raises `HTTPException` as starting one by hand
        would, or `CantStart`."""
        invocation = db.get(ChatInvocation, self.invocation_id)
        if invocation is None or invocation.status != ChatInvocationStatus.briefing:
            return None
        body = db.get(ChatMessage, invocation.message_id).body
        prompt = invocations.run_input(invocation.brief, body)
        organisation_id = invocations.organisation_of(db, invocation)
        with organisation_store(organisation_id) as store:
            agent = store.get(Agent, invocation.agent_id)
            if agent is None:
                raise firing.CantStart("The agent has been deleted.")
            # The agent's share lock (in the store) first, then the invocation's.
            run = agents_api.new_agent_run(
                store, organisation_id, agent, created_by=invocation.requested_by
            )
            invocation = invocations.lock(db, self.invocation_id)
            if invocation is None or invocation.status != ChatInvocationStatus.briefing:
                db.rollback()
                return None
            # The run first, in its store, then the invocation's link to it.
            store.commit()
            invocation.agent_run_id = run.id
            invocations.mark_running(db, self.invocation_id)
            db.commit()
            data_store_id = store_id_of(store)
        # Titled after what was asked.
        return firing.Turn(
            data_store_id, agent.id, run.id, prompt, title=invocations.message_text(body)
        )

    def beat(self, db: Session) -> None:
        invocations.beat(db, self.invocation_id)

    def record(self, db: Session, error: str | None) -> None:
        invocations.finish_turn(db, self.invocation_id, error)


def fail_stopped_turns(db: Session, chat_id: int | None = None) -> None:
    """Fail the agent invocations (the chat's, or all) whose turn's process has gone: their
    heartbeat stopped (`aios.firing`). Posts why, and fails their runs. Commits."""
    invocations.fail_stopped_turns(
        db, stale_after=firing.STALE_AFTER, error=firing.STOPPED, chat_id=chat_id
    )
    db.commit()


def _claim_turns(limit: int) -> list[int]:
    with SessionLocal() as db:
        return invocations.claim_turns(db, limit)


async def launch_turns() -> list[asyncio.Task]:
    """Claim the agent invocations waiting for a turn, as many as this process has free slots
    for under the cap, and run each in a fire of its own (`aios.firing.launch`). Those left wait
    for a slot here or on another replica: the scheduler's tick calls this too. None in a process
    kept from running agents (`scheduler_enabled`), which beats no fires."""
    free = firing.free_slots() if get_settings().scheduler_enabled else 0
    if free <= 0:
        return []
    claimed = await run_in_threadpool(_claim_turns, free)
    return [firing.launch(TurnFiring(i), name=f"invocation-{i}") for i in claimed]


async def start_invocations(invocation_ids: list[int]) -> None:
    """Start the workflow invocations' runs, each as a fire of its own (`aios.firing`), at once.
    Starting one only briefs it (one model call) and queues its run, so these don't wait for a
    free slot under the cap."""
    await asyncio.gather(
        *(firing.launch(InvocationFiring(i), name=f"invocation-{i}") for i in invocation_ids)
    )


async def run_turns() -> None:
    """Run the agent invocations waiting for a turn, as slots free up under the cap, until none
    is left waiting or every slot is taken (the scheduler's ticks take it from there)."""
    while fires := await launch_turns():
        await asyncio.gather(*fires)
