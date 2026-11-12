"""What the run service does with each kind of item (`aios.models.WorkKind`).

- **Invocation start**, a mention's: runs the handoff (`aios.handoff`), then enqueues the
  agent's turn, or starts the workflow's run and enqueues its first advance. An agent's
  mention waits its turn first: while an earlier mention of the agent in the chat hasn't
  finished (its turn is under way, or its run waits on an approval) it stays queued, and is
  started again when that one finishes (`aios.invocations`), so mentions run in message order.
- **Agent turn**: runs a mention's turn of the agent's conversation run in the chat (started
  by the first mention there), on the agent's current version, or a schedule's turn of the
  run it started when it fired (`aios.scheduler`): under the run's turn lease, through the turn
  code every turn shares (`aios.api.agents.turn_stream`). The reply, or why it failed, goes
  back to the chat, or onto the schedule.
- **Workflow advance** and **workflow node**: a workflow run moving on node by node
  (`aios.workflows.advance`). A node enqueues an advance when it's done.
- **Resume turn**: a standalone agent run waiting on schema change requests, carried on once
  one is decided (`aios.api.agents.resume_run`). A decision, or an expiry, enqueues it, or an
  advance of the workflow run whose node made the request (`enqueue_resume`).
- **Embedding sweep**: a store's search items waiting for an embedding, embedded under a time
  budget (`aios.search_index.embed_pending`); every process enqueues one per ready store each
  `search_embed_interval_seconds` (`aios.run_service.sweep.enqueue_embedding`).
- **Source sync**: one sync of a source (`aios.sync.run_sync`), in the `sources` lane; every
  process enqueues each ready store's sources due every `sync_poll_seconds`
  (`aios.run_service.sweep.enqueue_syncs`), and Sync now enqueues one at once.

Every handler can be run again for the same item: its process may have died part way, and the
item is then claimed again (`aios.run_service.queue`). So each reads where its invocation got
to, and carries on from there: a handoff already done isn't asked again, a run already started
isn't started twice, and a turn already begun carries on from the run's last checkpoint
(`aios.api.agents.Continue`) rather than starting over. An item claimed more times than it may
be tried fails its invocation (`fail`): "The service stopped mid-run."
"""

import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import HumanMessage
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from aios import (
    approvals,
    handback,
    handoff,
    invocations,
    jev,
    outbox,
    search_index,
    streams,
    sync,
    turn_lease,
    turns,
    workflows,
)
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_id_of,
    store_session,
)
from aios.datastores.models import (
    Agent,
    AgentRun,
    ChatMessage,
    Event,
    Invocation,
    SchemaChangeRequest,
    SyncKind,
    Trigger,
    Turn,
    Workflow,
    WorkflowRunStep,
)
from aios.db import SessionLocal
from aios.models import (
    AgentRunStatus,
    DataStore,
    InvocationStatus,
    TriggerStatus,
    TriggerTargetKind,
    WorkflowStepStatus,
    WorkKind,
    WorkLane,
)
from aios.run_service import queue
from aios.run_service.errors import STOPPED, CantStart
from aios.run_service.queue import Claimed, Work
from aios.tools import ToolOrganisation
from aios.workflows import structured

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


def start_work(
    organisation_id: int,
    data_store_id: uuid.UUID,
    invocation_id: int,
    *,
    again_if_running: bool = False,
) -> Work:
    """A mention's invocation start: in the interactive lane, people are waiting in the chat.
    `again_if_running` for a queued mention whose turn has come (`aios.invocations`): if its
    start is running now, finding it still has to wait, it runs once more."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.interactive,
        WorkKind.invocation_start,
        {"invocation_id": invocation_id},
        dedup_key=start_key(data_store_id, invocation_id),
        again_if_running=again_if_running,
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
    # The chat that follows it, if one does.
    chat_id: int | None = None


def _find(data_store_id: uuid.UUID, invocation_id: int) -> _Found | None:
    with store_session(data_store_id, "ro") as store:
        row = store.execute(
            select(
                Invocation.status,
                Invocation.target_kind,
                Invocation.agent_run_id,
                Invocation.workflow_run_id,
                Invocation.chat_id,
            ).where(Invocation.id == invocation_id)
        ).one_or_none()
    return None if row is None else _Found(*row)


def _waits_its_turn(data_store_id: uuid.UUID, invocation_id: int) -> bool:
    """Whether the store's mention of an agent waits for an earlier one in its chat
    (`invocations.queued_behind`)."""
    with store_session(data_store_id, "ro") as store:
        invocation = store.get(Invocation, invocation_id)
        return invocation is not None and invocations.queued_behind(store, invocation)


def _fail_unstarted(data_store_id: uuid.UUID, invocation_id: int, error: str) -> None:
    with store_session(data_store_id) as store:
        invocations.fail_unstarted(store, invocation_id, error)
        store.commit()


def _start_workflow_run(data_store_id: uuid.UUID, invocation_id: int) -> None:
    """Start the mentioned workflow's run with the brief and the message as its input, and
    enqueue its first advance; the run then finishes the invocation (`aios.invocations`).
    Nothing if it has a run already, or isn't briefing (it has finished). Raises `HTTPException`
    as starting one by hand would, or `CantStart`."""
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
            input_json=_mention_input(workflow, invocation.brief, message.body),
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
        workflows_api.queue_workflow_run(store, run, organisation_id)
        invocations.mark_running(store, invocation_id)
        store.commit()


def _mention_input(workflow: Workflow, brief: str | None, body: str) -> dict | None:
    """A mentioned workflow's input as JSON, if its current version has an input schema: the
    brief and the message as an object, each wrapped as untrusted as the text input's are
    (`invocations.run_input`). `CantStart` if the schema doesn't accept that shape."""
    schema = workflow.current_version.input_schema if workflow.current_version else None
    if schema is None:
        return None
    value = {
        "brief": jev.wrap(brief, "brief") if brief else "",
        "message": jev.wrap(invocations.message_text(body), invocations.CHAT_MESSAGE),
    }
    if errors := structured.schema_errors(value, schema):
        raise CantStart(
            "This workflow's input schema doesn't accept a brief and a message: "
            + structured.describe(errors)
        )
    return value


async def start_invocation(item: Claimed) -> Outcome:
    """A mention's invocation start (see the module docstring)."""
    store_id, invocation_id = item.data_store_id, item.payload["invocation_id"]
    found = await run_in_threadpool(_find, store_id, invocation_id)
    if found is None or found.status not in invocations.NOT_STARTED:
        return Done()
    if found.target_kind == TriggerTargetKind.agent and await run_in_threadpool(
        _waits_its_turn, store_id, invocation_id
    ):
        # Queued: started again when the mention it waits for finishes. Briefed only then, on
        # what the chat says since that turn.
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
    # Why the turn failed as it began: the agent's current version can't run.
    error: str | None = None


def _conversation_run(store: Session, agent_id: int, chat_id: int) -> AgentRun | None:
    """The agent's conversation run in the chat, locked until commit, if it has one."""
    return store.scalars(
        select(AgentRun)
        .where(AgentRun.agent_id == agent_id, AgentRun.chat_id == chat_id)
        .with_for_update()
    ).one_or_none()


def _start_mentioned_run(data_store_id: uuid.UUID, invocation_id: int) -> _Started | None:
    """Begin the mentioned agent's turn in its conversation run in the chat, with the brief and
    the message as the user's: the run is started (by whoever mentioned it) if the agent has
    none there yet, and otherwise moves to the agent's current version for this turn. The
    invocation records the run and goes running. A current version that can't run fails the
    turn as it begins, with why (`_Started.error`), and the run keeps its last version.

    None if the invocation has finished. Raises `HTTPException` as starting a run by hand would
    (the agent archived, say), or `CantStart`."""
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
        error = None
        # The agent's share lock first (an archived agent starts no turn), then the run's, then
        # the invocation's.
        agents_api.lock_live_agent(store, agent, read=True)
        run = _conversation_run(store, agent.id, invocation.chat_id)
        if run is None:
            run = agents_api.new_agent_run(
                store,
                organisation_id,
                agent,
                created_by=invocation.requested_by,
                by_mention=True,
                chat_id=invocation.chat_id,
            )
        else:
            try:
                agents_api.build_version_tools(
                    agent.current_version, ToolOrganisation(organisation_id, store)
                )
            except HTTPException as exc:
                error = str(exc.detail)
            else:
                run.agent_version_id = agent.current_version_id
        invocation = invocations.lock(store, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.briefing:
            store.rollback()
            return None
        # The run and the invocation's link to it, together.
        invocation.agent_run_id = run.id
        invocations.mark_running(store, invocation_id)
        if error is not None:
            turns.begin(
                store, run.id, invocation_id, error=error, version_id=agent.current_version_id
            )
        store.commit()
        # Titled after what was first asked.
        return _Started(agent.id, run.id, prompt, invocations.message_text(body), error)


@dataclass(frozen=True)
class _Progress:
    """Where an agent run's turn got to: its agent, and its latest turn, if it has begun one."""

    agent_id: int
    turn_id: int | None
    turn_status: AgentRunStatus | None
    turn_error: str | None


def _progress(data_store_id: uuid.UUID, run_id: int, invocation_id: int) -> _Progress | None:
    """Where the store's run's turn for the invocation got to (a conversation run has a turn
    for each of its mentions)."""
    with store_session(data_store_id, "ro") as store:
        agent_id = store.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))
        if agent_id is None:
            return None
        latest = store.execute(
            select(Turn.id, Turn.status, Turn.error)
            .where(Turn.agent_run_id == run_id, Turn.invocation_id == invocation_id)
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
    data_store_id: uuid.UUID,
    run_id: int,
    invocation_id: int,
    prompt: str,
    title: str | None,
    draft: streams.Draft | None = None,
) -> str | None | Later:
    """Run the store's run's turn for the invocation, or carry on the one an earlier attempt
    began; why it failed (None if it went fine), or `Later` if another turn holds the run.
    Raises `CantStart` if the run is gone or can't run. The reply's text as it's written goes to
    `draft`, if given: the chat following the turn shows it (`aios.streams`)."""
    progress = await run_in_threadpool(_progress, data_store_id, run_id, invocation_id)
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
                agents_api._start_turn,
                data_store_id,
                run_id,
                title or prompt,
                invocation_id=invocation_id,
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
        elif chunk["type"] == "text-delta" and draft is not None and draft.add(chunk["delta"]):
            await run_in_threadpool(draft.flush)
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
    draft: streams.Draft | None = None
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
            if started.error is not None:
                raise CantStart(started.error)
            run_id, prompt, title = started.run_id, started.prompt, started.title
        elif run_id is None:
            raise CantStart(invocations.RUN_DELETED)
        elif schedule is None:
            # Carrying on a mention's turn: its prompt is only needed if the run never got it.
            prompt = await run_in_threadpool(_mention_prompt, store_id, invocation_id)
        if found.chat_id is not None:
            # What its chat shows while the reply is written (`aios.streams`).
            draft = streams.Draft(store_id, found.chat_id, invocation_id)
        outcome = await _run_turn(store_id, run_id, invocation_id, prompt, title, draft)
        if isinstance(outcome, Later):
            return outcome
        error = outcome
    except CantStart as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    # The reply (or why it failed) is posted, and replaces the draft, which is gone.
    await run_in_threadpool(_record, store_id, invocation_id, schedule, error)
    if draft is not None:
        await run_in_threadpool(draft.clear)
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


def _fail_workflow(item: Claimed) -> list[Work]:
    """Give up on a workflow advance or node, its attempts used up: the node's step fails (and
    its turn), and the run advances to fail with it; an advance fails the run."""
    store_id, run_id = item.data_store_id, item.payload["run_id"]
    if item.kind == WorkKind.workflow_advance:
        workflows.fail_run(store_id, run_id, STOPPED)
        return []
    with store_session(store_id) as store:
        step = store.get(WorkflowRunStep, item.payload["step_id"])
        if step is not None and step.agent_run_id is not None:
            turns.fail_running(store, AgentRun.id == step.agent_run_id, error=STOPPED)
        store.commit()
    workflows._finish_step(
        store_id, item.payload["step_id"], WorkflowStepStatus.failed, None, STOPPED
    )
    return [workflows.advance_work(item.organisation_id, store_id, run_id)]


def _fail_resume(item: Claimed) -> None:
    """Give up on resuming an agent run, its attempts used up: its turn still marked running
    fails, and the chat whose mention started it is told."""
    store_id, run_id = item.data_store_id, item.payload["run_id"]
    with store_session(store_id) as store:
        turns.fail_running(store, AgentRun.id == run_id, error=STOPPED)
        invocations.finish_resumed(store, run_id, STOPPED)
        store.commit()


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


async def fail(item: Claimed) -> Outcome:
    """Give up on the item, its attempts used up (see each kind's)."""
    if item.kind in (WorkKind.embedding_sweep, WorkKind.source_sync):
        # Nothing waits on it: the next sweep embeds what's left; the next sync carries on
        # from the source's cursor, and its running record is failed once it's stale.
        return Done()
    if item.kind == WorkKind.handback:
        # The raw output is posted, with a note; the invocation still succeeds.
        await handback.give_up(item.data_store_id, item.payload["invocation_id"])
        return Done()
    if item.kind in (WorkKind.workflow_advance, WorkKind.workflow_node):
        return Done(await run_in_threadpool(_fail_workflow, item))
    if item.kind == WorkKind.resume_turn:
        await run_in_threadpool(_fail_resume, item)
        return Done()
    await run_in_threadpool(_fail, item)
    return Done()


async def workflow_advance(item: Claimed) -> Outcome:
    """A workflow run's advance (`aios.workflows.advance`)."""
    store_id, run_id = item.data_store_id, item.payload["run_id"]
    then, ended = await run_in_threadpool(
        workflows.advance_run, store_id, item.organisation_id, run_id
    )
    if ended is not None:
        await run_in_threadpool(workflows._record_ended, ended)
    return Done(then)


async def workflow_node(item: Claimed) -> Outcome:
    """An agent node's turn (`aios.workflows.run_node`); then its run advances."""
    store_id, run_id = item.data_store_id, item.payload["run_id"]
    moved = await workflows.run_node(
        store_id, run_id, item.payload["step_id"], bool(item.payload.get("resume"))
    )
    return Done([workflows.advance_work(item.organisation_id, store_id, run_id)] if moved else [])


def resume_key(data_store_id: uuid.UUID, run_id: int) -> str:
    return f"resume-turn:{data_store_id}:{run_id}"


def resume_work(organisation_id: int, data_store_id: uuid.UUID, run_id: int) -> Work:
    """Resuming the store's agent run: one queued per run at a time, and one decided while it
    runs makes it run again (the resume gives the run only the outcomes decided by then)."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.background,
        WorkKind.resume_turn,
        {"run_id": run_id},
        dedup_key=resume_key(data_store_id, run_id),
        again_if_running=True,
    )


async def resume_turn(item: Claimed) -> Outcome:
    """Carry on an agent run waiting on schema change requests, now some are decided. One that
    couldn't get the run to itself raises, and is tried again."""
    await agents_api.resume_run(item.data_store_id, item.payload["run_id"])
    return Done()


def resume_for(data_store_id: uuid.UUID, request_id: uuid.UUID) -> Work | None:
    """What carries on the run that made a request of the store, if it's decided and the run (if
    a run made it) hasn't been given the outcome yet: an advance of the workflow run whose agent
    node made it, which resumes the node where it waited; or a resume of the agent run."""
    with store_session(data_store_id, "ro") as store:
        request = store.get(SchemaChangeRequest, request_id)
        if request is None or not approvals.decided(request) or request.resumed_at is not None:
            return None
        run_id = request.requested_by_run
        workflow_run_id = workflows.workflow_run_waiting_on(store, request)
    if workflow_run_id is None and run_id is None:
        return None
    try:
        organisation_id = _organisation(data_store_id)
    except CantStart:
        return None
    if workflow_run_id is not None:
        return workflows.advance_work(organisation_id, data_store_id, workflow_run_id)
    return resume_work(organisation_id, data_store_id, run_id)


def enqueue_resume(data_store_id: uuid.UUID, request_id: uuid.UUID) -> None:
    """Enqueue carrying on the run that made a request of the store, once it's decided or has
    expired (`resume_for`), and wake the run service for it."""
    work = resume_for(data_store_id, request_id)
    if work is None:
        return
    with SessionLocal() as db:
        queue.enqueue(db, [work])
        db.commit()
    queue.wake(work.lane)


def handback_key(data_store_id: uuid.UUID, invocation_id: int) -> str:
    return f"handback:{data_store_id}:{invocation_id}"


def handback_work(organisation_id: int, data_store_id: uuid.UUID, invocation_id: int) -> Work:
    """Summarising an ended run's result and posting it (`aios.handback`): in the interactive
    lane, as people are waiting in the chat for it; one queued per invocation."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.interactive,
        WorkKind.handback,
        {"invocation_id": invocation_id},
        dedup_key=handback_key(data_store_id, invocation_id),
    )


async def hand_back(item: Claimed) -> Outcome:
    """A run's handback (`aios.handback.hand_back`): its summary, or its raw output if
    summarising failed, posted to its chat."""
    await handback.hand_back(item.data_store_id, item.payload["invocation_id"])
    return Done()


def embedding_key(data_store_id: uuid.UUID) -> str:
    return f"embedding-sweep:{data_store_id}"


def embedding_work(organisation_id: int, data_store_id: uuid.UUID) -> Work:
    """Embedding the store's search items waiting for an embedding: background work, so it never
    takes the slots kept for people waiting in chat; one queued per store at a time."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.background,
        WorkKind.embedding_sweep,
        {},
        dedup_key=embedding_key(data_store_id),
    )


def _embed(data_store_id: uuid.UUID) -> None:
    # Gone, going, behind or busy: the next sweep tries it again, if it's still there.
    with contextlib.suppress(DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
        search_index.embed_pending(data_store_id)


async def embedding_sweep(item: Claimed) -> Outcome:
    """Embed the store's search items waiting for an embedding, for up to the store's budget
    (`aios.search_index.embed_pending`); what's left waits for the next sweep, and is found by
    keyword meanwhile. An attempt cut off part way loses nothing: it's tried again."""
    await run_in_threadpool(_embed, item.data_store_id)
    return Done()


def sync_key(data_store_id: uuid.UUID, source_id: int, kind: SyncKind) -> str:
    return f"source-sync:{data_store_id}:{source_id}:{kind.value}"


def sync_work(
    organisation_id: int,
    data_store_id: uuid.UUID,
    source_id: int,
    kind: SyncKind = SyncKind.incremental,
) -> Work:
    """One sync of the store's source: sources work, claimed under the organisation's fair share
    of the `sources` lane; one queued per source and kind at a time."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.sources,
        WorkKind.source_sync,
        {"source_id": source_id, "kind": kind.value},
        dedup_key=sync_key(data_store_id, source_id, kind),
    )


def enqueue_sync(
    organisation_id: int, data_store_id: uuid.UUID, source_id: int, kind: SyncKind
) -> None:
    """Enqueue a sync of the store's source (Sync now, or a new source's first sync), and wake
    the run service for it."""
    from aios import run_service

    work = sync_work(organisation_id, data_store_id, source_id, kind)
    with SessionLocal() as db:
        queue.enqueue(db, [work])
        db.commit()
    run_service.wake(work.lane)


def _sync(data_store_id: uuid.UUID, source_id: int, kind: SyncKind) -> None:
    # Gone, going, behind or busy: the next poll enqueues it again, if it's still due.
    with contextlib.suppress(DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
        sync.run_sync(data_store_id, source_id, kind)


async def source_sync(item: Claimed) -> Outcome:
    """One sync of a source (`aios.sync.run_sync`); skipped if another of it is running."""
    kind = SyncKind(item.payload.get("kind", SyncKind.incremental.value))
    await run_in_threadpool(_sync, item.data_store_id, item.payload["source_id"], kind)
    return Done()


HANDLERS = {
    WorkKind.invocation_start: start_invocation,
    WorkKind.agent_turn: agent_turn,
    WorkKind.workflow_advance: workflow_advance,
    WorkKind.workflow_node: workflow_node,
    WorkKind.resume_turn: resume_turn,
    WorkKind.embedding_sweep: embedding_sweep,
    WorkKind.handback: hand_back,
    WorkKind.source_sync: source_sync,
}


def lost(store: Session, older_than_seconds: float) -> list[tuple[int, Work]]:
    """The store's invocations the run service owns that have waited unstarted for longer than
    `older_than_seconds` (a mention's from posting until its run starts, a schedule's agent turn
    until it begins), each with the item that carries it on: a mention's start, or a schedule's
    turn; and those handing back made as long ago, each with its handback. A mention of an agent
    queued behind an earlier unfinished one in its chat isn't lost: it's started when that one
    finishes (`invocations.queued_behind`). Whether each has an item queued is for the caller to
    check."""
    earlier = aliased(Invocation)
    store_id = store_id_of(store)
    organisation_id = _organisation(store_id)
    handing_back = store.scalars(
        select(Invocation.id).where(
            Invocation.status == InvocationStatus.handing_back,
            Invocation.created_at < func.now() - timedelta(seconds=older_than_seconds),
        )
    ).all()
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
            ~select(earlier.id)
            .where(
                earlier.chat_id == Invocation.chat_id,
                earlier.agent_id == Invocation.agent_id,
                earlier.id < Invocation.id,
                earlier.status.in_(invocations.LIVE),
            )
            .exists(),
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
            fired_at = datetime.fromtimestamp(0, UTC)
        else:
            fired_at = trigger.last_fired_at
        message = trigger.input
        if invocation.event_id is not None:
            # An event trigger's: told the event too (`aios.outbox.agent_message`).
            event = store.get(Event, invocation.event_id)
            message = (
                outbox.agent_message(trigger, outbox.event_document(event))
                if event
                else trigger.input
            )
        schedule = Schedule(trigger.id, fired_at, message)
        out.append((invocation.id, turn_work(organisation_id, store_id, invocation.id, schedule)))
    for invocation_id in handing_back:
        out.append((invocation_id, handback_work(organisation_id, store_id, invocation_id)))
    return out
