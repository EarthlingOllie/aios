"""What the run service does with each kind of item (`aios.models.WorkKind`).

- **Invocation start**, a mention's: runs the handoff (`aios.handoff`), then enqueues the
  agent's first turn, or starts the workflow's run and queues it for the worker.
- **Agent turn**: starts the mentioned agent's run, if it hasn't one yet, and runs one turn of
  it under the run's turn lease, through the turn code the chat endpoint uses
  (`aios.api.agents.turn_stream`); a schedule's run was started when the schedule fired
  (`aios.scheduler`). The reply, or why it failed, goes back to the chat, or onto the schedule.

Every handler can be run again for the same item: its process may have died part way, and the
item is then claimed again (`aios.run_service.queue`). So each reads where its invocation got
to, and carries on from there: a handoff already done isn't asked again, a run already started
isn't started twice, and a turn already begun carries on from the run's last checkpoint
(`aios.api.agents.Continue`) rather than starting over. An item claimed more times than it may
be tried fails its invocation (`fail`): "The service stopped mid-run."
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from aios import handoff, invocations, turn_lease, turns
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.datastores import store_id_of, store_session
from aios.datastores.models import (
    Agent,
    AgentRun,
    ChatMessage,
    Invocation,
    Trigger,
    Turn,
    Workflow,
)
from aios.db import SessionLocal
from aios.models import (
    AgentRunStatus,
    DataStore,
    InvocationStatus,
    TriggerStatus,
    TriggerTargetKind,
    WorkKind,
    WorkLane,
)
from aios.run_service.errors import STOPPED, CantStart
from aios.run_service.queue import Claimed, Work

log = logging.getLogger(__name__)

# How long a turn whose run is busy (another turn holds its lease) waits to be tried again.
BUSY_RETRY_SECONDS = 5.0


@dataclass
class Done:
    """The item is done; `then` is what to enqueue next, with its deletion."""

    then: list[Work] = field(default_factory=list)


@dataclass
class Later:
    """The item can't run yet: hand it back to run again after `delay_seconds`, without
    counting the attempt."""

    delay_seconds: float


Outcome = Done | Later


def start_key(data_store_id: uuid.UUID, invocation_id: int) -> str:
    return f"invocation-start:{data_store_id}:{invocation_id}"


def turn_key(data_store_id: uuid.UUID, invocation_id: int) -> str:
    return f"agent-turn:{data_store_id}:{invocation_id}"


def start_work(organisation_id: int, data_store_id: uuid.UUID, invocation_id: int) -> Work:
    """A mention's invocation start: in the interactive lane, people are waiting in the chat."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.interactive,
        WorkKind.invocation_start,
        {"invocation_id": invocation_id},
        dedup_key=start_key(data_store_id, invocation_id),
    )


@dataclass(frozen=True)
class Schedule:
    """The schedule firing a run was started for: its trigger, as claimed at `fired_at`, and the
    message its turn is given."""

    trigger_id: int
    fired_at: datetime
    message: str

    def payload(self) -> dict:
        return {
            "trigger_id": self.trigger_id,
            "fired_at": self.fired_at.isoformat(),
            "message": self.message,
        }

    @classmethod
    def of(cls, payload: dict) -> "Schedule | None":
        if "trigger_id" not in payload:
            return None
        return cls(
            payload["trigger_id"], datetime.fromisoformat(payload["fired_at"]), payload["message"]
        )


def turn_work(
    organisation_id: int,
    data_store_id: uuid.UUID,
    invocation_id: int,
    schedule: Schedule | None = None,
) -> Work:
    """An agent invocation's turn: a mention's in the interactive lane, a schedule's in the
    background one."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.background if schedule else WorkLane.interactive,
        WorkKind.agent_turn,
        {"invocation_id": invocation_id, **(schedule.payload() if schedule else {})},
        dedup_key=turn_key(data_store_id, invocation_id),
    )


def _organisation(data_store_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
    if organisation_id is None:
        raise CantStart("The organisation has been deleted.")
    return organisation_id


@dataclass(frozen=True)
class _Found:
    status: InvocationStatus
    target_kind: TriggerTargetKind
    agent_run_id: int | None
    workflow_run_id: int | None


def _find(data_store_id: uuid.UUID, invocation_id: int) -> _Found | None:
    with store_session(data_store_id, "ro") as store:
        row = store.execute(
            select(
                Invocation.status,
                Invocation.target_kind,
                Invocation.agent_run_id,
                Invocation.workflow_run_id,
            ).where(Invocation.id == invocation_id)
        ).one_or_none()
    return None if row is None else _Found(*row)


def _fail_unstarted(data_store_id: uuid.UUID, invocation_id: int, error: str) -> None:
    with store_session(data_store_id) as store:
        invocations.fail_unstarted(store, invocation_id, error)
        store.commit()


def _start_workflow_run(data_store_id: uuid.UUID, invocation_id: int) -> None:
    """Start the mentioned workflow's run with the brief and the message as its input, and queue
    it for the worker; the run then finishes the invocation (`aios.invocations`). Nothing if it
    has a run already, or isn't briefing (it has finished). Raises `HTTPException` as starting
    one by hand would, or `CantStart`."""
    organisation_id = _organisation(data_store_id)
    with store_session(data_store_id) as store:
        invocation = store.get(Invocation, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.briefing:
            return
        message = store.get_one(ChatMessage, invocation.message_id)
        workflow = store.get(Workflow, invocation.workflow_id)
        if workflow is None:
            raise CantStart("The workflow has been deleted.")
        # The workflow's share lock first, then the invocation's.
        run = workflows_api.new_workflow_run(
            store,
            organisation_id,
            workflow,
            invocations.run_input(invocation.brief, message.body),
            created_by=invocation.requested_by,
            by_mention=True,
        )
        invocation = invocations.lock(store, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.briefing:
            store.rollback()
            return
        invocation.workflow_run_id = run.id
        # Commits the run with the invocation's link to it, and queues it; if it can't be
        # queued the run is deleted, and the link with it.
        workflows_api.queue_workflow_run(store, run)
        invocations.mark_running(store, invocation_id)
        store.commit()


async def start_invocation(item: Claimed) -> Outcome:
    """A mention's invocation start (see the module docstring)."""
    store_id, invocation_id = item.data_store_id, item.payload["invocation_id"]
    found = await run_in_threadpool(_find, store_id, invocation_id)
    if found is None or found.status not in invocations.NOT_STARTED:
        return Done()
    try:
        await handoff.brief(store_id, invocation_id)
        if found.target_kind == TriggerTargetKind.agent:
            return Done([turn_work(item.organisation_id, store_id, invocation_id)])
        await run_in_threadpool(_start_workflow_run, store_id, invocation_id)
    except CantStart as exc:
        await run_in_threadpool(_fail_unstarted, store_id, invocation_id, str(exc))
    except HTTPException as exc:
        await run_in_threadpool(_fail_unstarted, store_id, invocation_id, str(exc.detail))
    return Done()


@dataclass(frozen=True)
class _Started:
    agent_id: int
    run_id: int
    prompt: str
    title: str | None


def _start_mentioned_run(data_store_id: uuid.UUID, invocation_id: int) -> _Started | None:
    """Start a run of the mentioned agent's current version, started by whoever mentioned it,
    with the brief and the message as the user's; the invocation records it and goes running.
    None if it has finished. Raises `HTTPException` as starting one by hand would, or
    `CantStart`."""
    organisation_id = _organisation(data_store_id)
    with store_session(data_store_id) as store:
        invocation = store.get(Invocation, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.briefing:
            return None
        body = store.get_one(ChatMessage, invocation.message_id).body
        prompt = invocations.run_input(invocation.brief, body)
        agent = store.get(Agent, invocation.agent_id)
        if agent is None:
            raise CantStart("The agent has been deleted.")
        # The agent's share lock first, then the invocation's.
        run = agents_api.new_agent_run(
            store, organisation_id, agent, created_by=invocation.requested_by, by_mention=True
        )
        invocation = invocations.lock(store, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.briefing:
            store.rollback()
            return None
        # The run and the invocation's link to it, together.
        invocation.agent_run_id = run.id
        invocations.mark_running(store, invocation_id)
        store.commit()
        # Titled after what was asked.
        return _Started(agent.id, run.id, prompt, invocations.message_text(body))


@dataclass(frozen=True)
class _Progress:
    """Where an agent run's turn got to: its agent, and its latest turn, if it has begun one."""

    agent_id: int
    turn_id: int | None
    turn_status: AgentRunStatus | None
    turn_error: str | None


def _progress(data_store_id: uuid.UUID, run_id: int) -> _Progress | None:
    with store_session(data_store_id, "ro") as store:
        agent_id = store.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))
        if agent_id is None:
            return None
        latest = store.execute(
            select(Turn.id, Turn.status, Turn.error)
            .where(Turn.agent_run_id == run_id)
            .order_by(Turn.id.desc())
            .limit(1)
        ).one_or_none()
    if latest is None:
        return _Progress(agent_id, None, None, None)
    return _Progress(agent_id, *latest)


def _context(data_store_id: uuid.UUID, agent_id: int, run_id: int) -> agents_api.ChatContext:
    with SessionLocal() as db:
        return agents_api.load_chat_context(db, data_store_id, agent_id, run_id)


async def _run_turn(
    data_store_id: uuid.UUID, run_id: int, prompt: str, title: str | None
) -> str | None | Later:
    """Run the store's run's turn, or carry on the one an earlier attempt began; why it failed
    (None if it went fine), or `Later` if another turn holds the run. Raises `CantStart` if the
    run is gone or can't run."""
    progress = await run_in_threadpool(_progress, data_store_id, run_id)
    if progress is None:
        raise CantStart(invocations.RUN_DELETED)
    if progress.turn_id is not None and progress.turn_status != AgentRunStatus.running:
        # The turn ended, but its process stopped before saying how it went.
        return progress.turn_error
    try:
        ctx = await run_in_threadpool(_context, data_store_id, progress.agent_id, run_id)
    except HTTPException as exc:
        raise CantStart(str(exc.detail)) from exc
    try:
        lease = await turn_lease.acquire(data_store_id, run_id)
    except turn_lease.RunBusy:
        return Later(BUSY_RETRY_SECONDS)
    except turn_lease.RunGone as exc:
        raise CantStart(invocations.RUN_DELETED) from exc
    message = HumanMessage(content=prompt)
    turn: HumanMessage | agents_api.Continue
    try:
        if progress.turn_id is None:
            turn_id = await run_in_threadpool(
                agents_api._start_turn, data_store_id, run_id, title or prompt
            )
            turn = message
        else:
            turn_id, turn = progress.turn_id, agents_api.Continue(message)
    except BaseException:
        await turn_lease.release(lease)
        raise
    error = None
    async for chunk in agents_api.turn_stream(ctx, run_id, turn, lease, turn_id=turn_id):
        if chunk["type"] == "error":
            error = chunk["errorText"]
    return error


def _record_mention(data_store_id: uuid.UUID, invocation_id: int, error: str | None) -> None:
    with store_session(data_store_id) as store:
        invocations.finish_turn(store, invocation_id, error)
        store.commit()


def _record_schedule(data_store_id: uuid.UUID, schedule: Schedule, error: str | None) -> None:
    """Record how the schedule's firing went on its trigger: a no-op if the trigger was deleted,
    or has fired again since."""
    with store_session(data_store_id) as store:
        store.execute(
            update(Trigger)
            .where(Trigger.id == schedule.trigger_id, Trigger.last_fired_at == schedule.fired_at)
            .values(
                last_status=TriggerStatus.failed if error else TriggerStatus.succeeded,
                last_error=error,
            )
        )
        store.commit()


async def agent_turn(item: Claimed) -> Outcome:
    """An agent invocation's turn (see the module docstring)."""
    store_id, invocation_id = item.data_store_id, item.payload["invocation_id"]
    schedule = Schedule.of(item.payload)
    found = await run_in_threadpool(_find, store_id, invocation_id)
    if found is None or found.status not in invocations.UNFINISHED:
        return Done()
    error: str | None
    try:
        run_id = found.agent_run_id
        prompt = schedule.message if schedule else ""
        title = None
        if run_id is None and schedule is None:
            # A mention's: briefed (a no-op if it was), then its run starts.
            await handoff.brief(store_id, invocation_id)
            started = await run_in_threadpool(_start_mentioned_run, store_id, invocation_id)
            if started is None:
                return Done()
            run_id, prompt, title = started.run_id, started.prompt, started.title
        elif run_id is None:
            raise CantStart(invocations.RUN_DELETED)
        elif schedule is None:
            # Carrying on a mention's turn: its prompt is only needed if the run never got it.
            prompt = await run_in_threadpool(_mention_prompt, store_id, invocation_id)
        outcome = await _run_turn(store_id, run_id, prompt, title)
        if isinstance(outcome, Later):
            return outcome
        error = outcome
    except CantStart as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    await run_in_threadpool(_record, store_id, invocation_id, schedule, error)
    return Done()


def _mention_prompt(data_store_id: uuid.UUID, invocation_id: int) -> str:
    with store_session(data_store_id, "ro") as store:
        invocation = store.get_one(Invocation, invocation_id)
        body = store.get_one(ChatMessage, invocation.message_id).body
        return invocations.run_input(invocation.brief, body)


def _record(
    data_store_id: uuid.UUID, invocation_id: int, schedule: Schedule | None, error: str | None
) -> None:
    if schedule is None:
        _record_mention(data_store_id, invocation_id, error)
        return
    if error is not None:
        # A turn that ended finishes its own invocation; one that never ran is failed here.
        with store_session(data_store_id) as store:
            invocation = invocations.lock(store, invocation_id)
            if invocation is not None:
                invocations.failed(store, invocation, error)
            store.commit()
    _record_schedule(data_store_id, schedule, error)


def _fail(item: Claimed) -> None:
    """Give up on the item, its attempts used up: fail its invocation, its run's turn still
    marked running, and its schedule's firing, with `STOPPED`."""
    store_id, invocation_id = item.data_store_id, item.payload["invocation_id"]
    schedule = Schedule.of(item.payload)
    with store_session(store_id) as store:
        run_id = store.scalar(select(Invocation.agent_run_id).where(Invocation.id == invocation_id))
        if run_id is not None:
            turns.fail_running(store, AgentRun.id == run_id, error=STOPPED)
        if item.kind == WorkKind.invocation_start:
            invocations.fail_unstarted(store, invocation_id, STOPPED)
        elif schedule is None:
            invocations.finish_turn(store, invocation_id, STOPPED)
        else:
            invocation = invocations.lock(store, invocation_id)
            if invocation is not None:
                invocations.failed(store, invocation, STOPPED)
        store.commit()
    if schedule is not None:
        _record_schedule(store_id, schedule, STOPPED)


async def fail(item: Claimed) -> None:
    await run_in_threadpool(_fail, item)


HANDLERS = {
    WorkKind.invocation_start: start_invocation,
    WorkKind.agent_turn: agent_turn,
}


def lost(store: Session, older_than_seconds: float) -> list[tuple[int, Work]]:
    """The store's invocations the run service owns that have waited unstarted for longer than
    `older_than_seconds` (a mention's from posting until its run starts, a schedule's agent turn
    until it begins), each with the item that carries it on: a mention's start, or a schedule's
    turn. Whether each has an item queued is for the caller to check."""
    store_id = store_id_of(store)
    organisation_id = _organisation(store_id)
    found = store.execute(
        select(Invocation, Trigger)
        .outerjoin(
            Trigger,
            and_(
                Trigger.id == Invocation.trigger_id,
                Trigger.last_run_id == Invocation.agent_run_id,
            ),
        )
        .where(
            Invocation.status.in_(invocations.NOT_STARTED),
            Invocation.created_at < func.now() - timedelta(seconds=older_than_seconds),
            Invocation.parent_id.is_(None),
            or_(
                Invocation.chat_id.is_not(None),
                and_(
                    Invocation.trigger_id.is_not(None),
                    Invocation.target_kind == TriggerTargetKind.agent,
                    Invocation.agent_run_id.is_not(None),
                ),
            ),
        )
    ).all()
    out = []
    for invocation, trigger in found:
        if invocation.chat_id is not None:
            out.append((invocation.id, start_work(organisation_id, store_id, invocation.id)))
            continue
        if trigger is None:
            # Fired again since, or edited: the turn still runs, recorded on nothing.
            trigger = store.get_one(Trigger, invocation.trigger_id)
            schedule = Schedule(trigger.id, datetime.fromtimestamp(0, UTC), trigger.input)
        else:
            schedule = Schedule(trigger.id, trigger.last_fired_at, trigger.input)
        out.append((invocation.id, turn_work(organisation_id, store_id, invocation.id, schedule)))
    return out
