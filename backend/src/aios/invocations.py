"""What a mention in a chat starts: an invocation of an agent or workflow (`ChatInvocation`).

Posting a message that mentions an agent or workflow member creates, in the message's own
transaction, a queued invocation and the member's acknowledgement: a run card (`create`), so the
chat never shows a mention without its card. Only a user's message does this: what an agent or
workflow posts (a result) starts nothing, whatever it mentions, so a chat can't loop.

For a workflow, after the response, a background step starts the run
(`aios.api.chats.InvocationFiring`). First the handoff (`aios.handoff`) briefs it: the invocation
goes `briefing` (`begin_briefing`), and one model call writes the brief, kept on the invocation,
and the ack, which replaces the acknowledgement's placeholder (`record_handoff`). Then the run
starts through the helpers the UI and schedules use, with the brief and the message as its input
(`run_input`), started by the member who mentioned it (`requested_by`). The invocation records
the run, and goes `running` once it's queued (`mark_running`). A start that's refused, or a
handoff that fails, fails it with the reason (`fail_unstarted`).

Every invocation ends the same way, in one transaction: it's marked succeeded or failed and its
result is posted back as a message from the agent or workflow, belonging to the invocation
(`_finish`), so whoever is following the card sees it end with the message. A workflow run ends
in the worker or when it's found stopped (`aios.workflows._end_runs`, which calls
`finish_workflow_runs`), or is deleted before it does (`fail_deleted_run`). An invocation that never
got as far as starting, because its process stopped first, is failed when the chat is next read
(`fail_stalled`).

Mentioning an agent queues an invocation the same way, but its turn runs in an API process
(`aios.api.chats.TurnFiring`), under the firing machinery's cap: it waits, queued, until a
process with a free slot claims it (`claim_turns`), which beats it while the handoff and then the
turn run (`beat`).
When the turn ends its reply, or why it failed, is posted back (`finish_turn`); one whose
heartbeat has stopped lost its process, and is failed (`fail_stopped_turns`). A turn that ends
waiting for a member to decide its schema change request leaves the invocation running, its card
showing the run waiting, with no process beating it; the run's resume, once the request is
decided, finishes it (`finish_resumed`).

A run an invocation started is the organisation's, like any other: started by the member who
mentioned it, executed for the organisation, and read by every member (`aios.api.agents`).
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import timedelta

from sqlalchemy import ColumnElement, func, select, true, update
from sqlalchemy.orm import Session

from aios import checkpoints
from aios.datastores import organisation_store, store_session
from aios.datastores.models import Agent, AgentRun, Workflow, WorkflowRun
from aios.datastores.schema_changes import not_given
from aios.models import (
    AgentRunStatus,
    Chat,
    ChatAuthorKind,
    ChatInvocation,
    ChatInvocationStatus,
    ChatMessage,
    ChatMessageKind,
    DataStore,
    SchemaChangeRequest,
    Trigger,
    TriggerTargetKind,
    WorkflowRunStatus,
)

# The acknowledgement a run card carries until the handoff replaces it with what the member
# understood (`aios.handoff`).
ACKNOWLEDGEMENT = "Reading the chat…"
# How long an invocation can wait to start before it counts as lost with its process: the start
# runs straight after the message is posted and takes moments.
START_WITHIN = timedelta(seconds=90)
NEVER_STARTED = "The service stopped before the run could start."
RUN_DELETED = "The run was deleted before it finished."
NO_OUTPUT = "_The run finished with no output._"
NO_REPLY = "_The agent finished without a reply._"

# Before its run has started, and before it has finished.
NOT_STARTED = (ChatInvocationStatus.queued, ChatInvocationStatus.briefing)
UNFINISHED = (*NOT_STARTED, ChatInvocationStatus.running)

# A mention's token in a message (`@[name](kind:id)`, the name with `\`, `[` and `]` escaped).
TOKEN = re.compile(r"@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):[1-9]\d*\)")


def message_text(body: str) -> str:
    """A message's text, with each mention as `@name`."""
    return TOKEN.sub(lambda m: "@" + re.sub(r"\\(.)", r"\1", m.group(1)), body)


def run_input(brief: str | None, body: str) -> str:
    """A run's input: the handoff's brief, a blank line, then the message's text (just the
    text if the chat had nothing to brief it on)."""
    text = message_text(body)
    return f"{brief}\n\n{text}" if brief else text


def create(
    db: Session, message: ChatMessage, targets: Sequence[tuple[TriggerTargetKind, int]]
) -> list[ChatInvocation]:
    """Queue an invocation of each agent or workflow (kind, id) the (just added) message
    mentions, each with its acknowledgement and card; in the caller's transaction. Nothing for a
    message that isn't a user's. An archived agent's or workflow's fails at once ("The agent
    <name> is archived"), before any handoff, so no model is called for it."""
    if message.author_kind != ChatAuthorKind.user:
        return []
    db.flush()
    made = []
    organisation_id = db.scalar(select(Chat.organisation_id).where(Chat.id == message.chat_id))
    targets = list(targets)
    archived = _archived(organisation_id, targets)
    for kind, target_id in targets:
        is_agent = kind == TriggerTargetKind.agent
        invocation = ChatInvocation(
            chat_id=message.chat_id,
            message_id=message.id,
            requested_by=message.author_user_id,
            target_kind=kind,
            agent_id=target_id if is_agent else None,
            workflow_id=None if is_agent else target_id,
            status=ChatInvocationStatus.queued,
        )
        db.add(invocation)
        db.flush()
        db.add(
            ChatMessage(
                chat_id=message.chat_id,
                author_kind=ChatAuthorKind(kind.value),
                author_agent_id=invocation.agent_id,
                author_workflow_id=invocation.workflow_id,
                kind=ChatMessageKind.run_card,
                body=ACKNOWLEDGEMENT,
                invocation_id=invocation.id,
            )
        )
        db.flush()
        if (name := archived.get((kind, target_id))) is not None:
            fail_unstarted(db, invocation.id, f"The {kind.value} {name} is archived")
        made.append(invocation)
    return made


def _archived(
    organisation_id: int, targets: Sequence[tuple[TriggerTargetKind, int]]
) -> dict[tuple[TriggerTargetKind, int], str]:
    """The names of these agents and workflows (kind, id) of the organisation's that are
    archived, from its store, by (kind, id)."""
    found: dict[tuple[TriggerTargetKind, int], str] = {}
    if not targets:
        return found
    with organisation_store(organisation_id, "ro") as store:
        for kind, model in (
            (TriggerTargetKind.agent, Agent),
            (TriggerTargetKind.workflow, Workflow),
        ):
            ids = [i for k, i in targets if k == kind]
            if not ids:
                continue
            for i, name in store.execute(
                select(model.id, model.name).where(
                    model.id.in_(ids), model.archived_at.is_not(None)
                )
            ):
                found[kind, i] = name
    return found


def organisation_of(db: Session, invocation: ChatInvocation) -> int:
    """The organisation the invocation's chat is in: whose store its agent or workflow is in."""
    return db.scalar(select(Chat.organisation_id).where(Chat.id == invocation.chat_id))


def chat_store(chat_id) -> ColumnElement:
    """The data store of the chat (an id, or a column of one): its organisation's, where the runs
    its invocations started are. Run ids are each store's own, so an invocation's run is named
    by both."""
    return (
        select(DataStore.id)
        .join(Chat, Chat.organisation_id == DataStore.owner_organisation_id)
        .where(Chat.id == chat_id)
        .scalar_subquery()
    )


def store_of(db: Session, invocation: ChatInvocation) -> uuid.UUID | None:
    """The data store the invocation's runs are in."""
    return db.scalar(select(chat_store(invocation.chat_id)))


def in_store(data_store_id: uuid.UUID) -> ColumnElement[bool]:
    """Whether an invocation is of a chat whose runs are in the store."""
    return chat_store(ChatInvocation.chat_id) == data_store_id


def forget_runs(
    db: Session,
    data_store_id: uuid.UUID,
    *,
    agent_run_ids: Iterable[int] = (),
    workflow_run_ids: Iterable[int] = (),
) -> None:
    """What on the platform names these runs of the store, which have been deleted there, stops
    naming them (there's no foreign key to do it): the invocations that started them (whose
    cards then say the run was deleted), the schedules that last started them, and the schema
    change requests they made. In the caller's transaction, once the deletion has committed in
    the store."""
    agent_run_ids, workflow_run_ids = list(agent_run_ids), list(workflow_run_ids)
    organisation_id = select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
    if agent_run_ids:
        db.execute(
            update(ChatInvocation)
            .where(ChatInvocation.agent_run_id.in_(agent_run_ids), in_store(data_store_id))
            .values(agent_run_id=None)
        )
        db.execute(
            update(Trigger)
            .where(
                Trigger.last_run_id.in_(agent_run_ids),
                Trigger.organisation_id.in_(organisation_id),
            )
            .values(last_run_id=None)
        )
        db.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.data_store_id == data_store_id,
                SchemaChangeRequest.requested_by_run.in_(agent_run_ids),
            )
            .values(requested_by_run=None)
        )
    if workflow_run_ids:
        db.execute(
            update(ChatInvocation)
            .where(ChatInvocation.workflow_run_id.in_(workflow_run_ids), in_store(data_store_id))
            .values(workflow_run_id=None)
        )
        db.execute(
            update(Trigger)
            .where(
                Trigger.last_workflow_run_id.in_(workflow_run_ids),
                Trigger.organisation_id.in_(organisation_id),
            )
            .values(last_workflow_run_id=None)
        )


def lock(db: Session, invocation_id: int) -> ChatInvocation | None:
    """The invocation, locked until commit, if it hasn't finished."""
    return db.scalars(
        select(ChatInvocation)
        .where(ChatInvocation.id == invocation_id, ChatInvocation.status.in_(UNFINISHED))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def begin_briefing(db: Session, invocation_id: int) -> ChatInvocation | None:
    """The queued invocation, locked and now briefing (`aios.handoff`); None if it isn't
    queued. In the caller's transaction."""
    invocation = lock(db, invocation_id)
    if invocation is None or invocation.status != ChatInvocationStatus.queued:
        return None
    invocation.status = ChatInvocationStatus.briefing
    return invocation


def record_handoff(db: Session, invocation_id: int, ack: str, brief: str) -> None:
    """Keep the handoff's brief for the run, and put its ack in place of the acknowledgement's
    placeholder; in the caller's transaction. A no-op unless it's still briefing."""
    invocation = lock(db, invocation_id)
    if invocation is None or invocation.status != ChatInvocationStatus.briefing:
        return
    invocation.brief = brief
    if ack:
        db.execute(
            update(ChatMessage)
            .where(
                ChatMessage.invocation_id == invocation_id,
                ChatMessage.kind == ChatMessageKind.run_card,
            )
            .values(body=ack)
        )


def mark_running(db: Session, invocation_id: int) -> None:
    """Its run has been queued: it's running. A no-op if the run has already ended it."""
    db.execute(
        update(ChatInvocation)
        .where(
            ChatInvocation.id == invocation_id,
            ChatInvocation.status.in_(NOT_STARTED),
        )
        .values(
            status=ChatInvocationStatus.running,
            started_at=func.coalesce(ChatInvocation.started_at, func.now()),
        )
    )


def _finish(
    db: Session, invocation: ChatInvocation, status: ChatInvocationStatus, body: str, error=None
) -> None:
    """End the (locked, unfinished) invocation and post its result back into the chat."""
    invocation.status = status
    invocation.error = error
    invocation.finished_at = func.now()
    db.add(
        ChatMessage(
            chat_id=invocation.chat_id,
            author_kind=ChatAuthorKind(invocation.target_kind.value),
            author_workflow_id=invocation.workflow_id,
            author_agent_id=invocation.agent_id,
            kind=ChatMessageKind.text,
            body=body,
            invocation_id=invocation.id,
        )
    )
    # A result is activity.
    db.execute(update(Chat).where(Chat.id == invocation.chat_id).values(updated_at=func.now()))


def failed(db: Session, invocation: ChatInvocation, error: str) -> None:
    _finish(db, invocation, ChatInvocationStatus.failed, f"The run failed: {error}", error)


def fail_unstarted(db: Session, invocation_id: int, error: str) -> None:
    """Fail an invocation whose run couldn't start, with why; in the caller's transaction. A
    no-op once its run has started (it finishes when the run does), or it has finished."""
    invocation = lock(db, invocation_id)
    if invocation is None or invocation.status not in NOT_STARTED:
        return
    _finish(
        db,
        invocation,
        ChatInvocationStatus.failed,
        f"Couldn't start the run: {error}",
        error,
    )


def finish_workflow_runs(
    db: Session, data_store_id: uuid.UUID, runs: Sequence[WorkflowRun]
) -> None:
    """Finish the invocations of these workflow runs of the store, which have just ended (as
    they are now there), with how each went; in the caller's transaction."""
    by_id = {run.id: run for run in runs}
    if not by_id:
        return
    invocations = db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.workflow_run_id.in_(list(by_id)),
            in_store(data_store_id),
            ChatInvocation.status.in_(UNFINISHED),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for invocation in invocations:
        run = by_id[invocation.workflow_run_id]
        # Started, even if it ended before it was marked running.
        invocation.started_at = func.coalesce(ChatInvocation.started_at, run.started_at, func.now())
        if run.status == WorkflowRunStatus.succeeded:
            _finish(db, invocation, ChatInvocationStatus.succeeded, run.output or NO_OUTPUT)
        else:
            failed(db, invocation, run.error or "The run failed.")


def fail_deleted_run(db: Session, data_store_id: uuid.UUID, workflow_run_id: int) -> None:
    """Fail the invocation of the store's workflow run, deleted before it finished; in the
    caller's transaction."""
    for invocation in db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.workflow_run_id == workflow_run_id,
            in_store(data_store_id),
            ChatInvocation.status.in_(UNFINISHED),
        )
        .with_for_update()
    ):
        failed(db, invocation, RUN_DELETED)


def fail_stalled(db: Session, chat_id: int) -> None:
    """Fail the chat's workflow invocations that never started and never will: still queued or
    briefing without a run long after they were made, their process gone. Commits. (An agent's
    waits for a free slot, however long; see `claim_turns`.)"""
    for invocation in db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.chat_id == chat_id,
            ChatInvocation.target_kind == TriggerTargetKind.workflow,
            ChatInvocation.status.in_(NOT_STARTED),
            ChatInvocation.workflow_run_id.is_(None),
            ChatInvocation.agent_run_id.is_(None),
            ChatInvocation.created_at < func.now() - START_WITHIN,
        )
        .with_for_update(skip_locked=True)
    ):
        failed(db, invocation, NEVER_STARTED)
    db.commit()


def claim_turns(db: Session, limit: int) -> list[int]:
    """Claim up to `limit` of the agent invocations waiting for a process to run their turn, the
    oldest first, for this process to run: each gets its heartbeat, which the process keeps
    while it runs the turn. Commits; returns their ids."""
    claimed = db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.target_kind == TriggerTargetKind.agent,
            ChatInvocation.status == ChatInvocationStatus.queued,
            ChatInvocation.heartbeat_at.is_(None),
        )
        .order_by(ChatInvocation.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for invocation in claimed:
        invocation.heartbeat_at = func.now()
    db.commit()
    return [i.id for i in claimed]


def beat(db: Session, invocation_id: int) -> None:
    """Refresh a claimed invocation's heartbeat while its turn runs; in the caller's
    transaction. Not once it has finished."""
    db.execute(
        update(ChatInvocation)
        .where(ChatInvocation.id == invocation_id, ChatInvocation.status.in_(UNFINISHED))
        .values(heartbeat_at=func.now())
    )


def last_reply(data_store_id: uuid.UUID, run_id: int) -> str:
    """The agent's final reply in the store's run: its last message, if that's the model's."""
    history = checkpoints.history(data_store_id, run_id)
    if history and history[-1].type == "ai" and history[-1].text.strip():
        return history[-1].text
    return NO_REPLY


def finish_turn(db: Session, invocation_id: int, error: str | None) -> None:
    """Finish an agent invocation whose turn has ended (`error` None if it went fine): post the
    agent's reply, or why it failed; in the caller's transaction. A no-op if it has already
    finished (failed as stalled, say)."""
    invocation = lock(db, invocation_id)
    if invocation is None:
        return
    if invocation.status in NOT_STARTED:
        # The run never started.
        _finish(
            db,
            invocation,
            ChatInvocationStatus.failed,
            f"Couldn't start the run: {error or NEVER_STARTED}",
            error or NEVER_STARTED,
        )
    elif invocation.agent_run_id is None:
        failed(db, invocation, RUN_DELETED)
    elif error is not None:
        failed(db, invocation, error)
    else:
        store_id = store_of(db, invocation)
        if not db.scalar(select(waiting_on_a_request(invocation.agent_run_id, store_id))):
            reply = last_reply(store_id, invocation.agent_run_id)
            _finish(db, invocation, ChatInvocationStatus.succeeded, reply)
    # Otherwise it waits, running, for the run's resume to finish it (`finish_resumed`).


def waiting_on_a_request(run_id, data_store_id):
    """Whether the agent run (an id, or a column of one) of the store (an id, or an expression
    of one) waits on a schema change request: not yet resumed with its outcome."""
    return (
        select(SchemaChangeRequest.id)
        .where(
            SchemaChangeRequest.data_store_id == data_store_id,
            SchemaChangeRequest.requested_by_run == run_id,
            *not_given(),
        )
        .exists()
    )


def finish_resumed(db: Session, data_store_id: uuid.UUID, run_id: int, error: str | None) -> None:
    """Finish the agent invocation whose run (the store's) waited on a schema change request,
    now the run has been resumed (`error` None if it went fine), as `finish_turn` does: unless it
    waits on another. In the caller's transaction; nothing for a run no invocation is waiting
    on."""
    invocation_id = db.scalar(
        select(ChatInvocation.id).where(
            ChatInvocation.agent_run_id == run_id,
            in_store(data_store_id),
            ChatInvocation.status == ChatInvocationStatus.running,
        )
    )
    if invocation_id is not None:
        finish_turn(db, invocation_id, error)


def fail_stopped_turns(
    db: Session, *, stale_after: timedelta, error: str, chat_id: int | None = None
) -> None:
    """Fail the unfinished agent invocations (the chat's, or all) whose heartbeat has stopped for
    `stale_after`, their process gone, with `error`; and their runs, if still marked running.
    In the caller's transaction."""
    for invocation in db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.target_kind == TriggerTargetKind.agent,
            ChatInvocation.status.in_(UNFINISHED),
            ChatInvocation.heartbeat_at < func.now() - stale_after,
            # No process beats a run waiting for a member's decision.
            ~waiting_on_a_request(ChatInvocation.agent_run_id, chat_store(ChatInvocation.chat_id)),
            true() if chat_id is None else ChatInvocation.chat_id == chat_id,
        )
        .with_for_update(skip_locked=True)
    ):
        if invocation.agent_run_id is not None and (store_id := store_of(db, invocation)):
            _fail_running(store_id, invocation.agent_run_id)
        failed(db, invocation, error)


def _fail_running(data_store_id: uuid.UUID, run_id: int) -> None:
    """Fail the store's run, if it's still marked running (its process has gone)."""
    with store_session(data_store_id) as store:
        store.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == AgentRunStatus.running)
            .values(status=AgentRunStatus.failed, finished_at=func.now())
        )
        store.commit()
