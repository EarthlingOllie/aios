"""Why each run ran: an invocation of an agent or workflow (`Invocation`, in the organisation's
store). Every run has exactly one, made with it: a mention's (cause mention), a schedule's
(schedule, naming its trigger) or a person's starting it by hand (manual), and a workflow agent
node's, a child of its workflow run's with that one's cause (`for_run`). A child carries the
chain of its ancestors' ids and its depth, for loops to be stopped by.

An invocation no chat follows (any but a mention's) is queued until its run starts, running
while it goes, and finished, with no message posted, when an agent run's first turn ends
(`finish_quietly`) or a workflow run ends (`finish_workflow_runs`). Later turns of the run,
from its page, are the same invocation's.

What a mention in a chat starts:

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

Chats, their messages, invocations, runs and schema change requests are all in the
organisation's store, so everything here is on a session on it (`store`), in one transaction:
a run deleted clears the invocation's link to it by its foreign key.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session

from aios import checkpoints, jev, search_index
from aios.datastores import store_id_of
from aios.datastores.models import (
    Agent,
    AgentRun,
    Chat,
    ChatMessage,
    Invocation,
    SchemaChangeRequest,
    Workflow,
    WorkflowRun,
)
from aios.datastores.schema_changes import not_given
from aios.models import (
    ChatAuthorKind,
    ChatMessageKind,
    InvocationCause,
    InvocationStatus,
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
NOT_STARTED = (InvocationStatus.queued, InvocationStatus.briefing)
UNFINISHED = (*NOT_STARTED, InvocationStatus.running)

# A mention's token in a message (`@[name](kind:id)`, the name with `\`, `[` and `]` escaped).
TOKEN = re.compile(r"@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):[1-9]\d*\)")


def message_text(body: str) -> str:
    """A message's text, with each mention as `@name`."""
    return TOKEN.sub(lambda m: "@" + re.sub(r"\\(.)", r"\1", m.group(1)), body)


def run_input(brief: str | None, body: str) -> str:
    """A run's input: the handoff's brief, a blank line, then the message's text (just the
    text if the chat had nothing to brief it on). The brief is derived text, written by the
    handoff's model from the chat, so it's wrapped as untrusted (`aios.jev.wrap`)."""
    text = message_text(body)
    return f"{jev.wrap(brief, 'brief')}\n\n{text}" if brief else text


def for_run(
    store: Session,
    kind: TriggerTargetKind,
    target_id: int,
    *,
    requested_by: int | None,
    trigger_id: int | None = None,
    parent: Invocation | None = None,
    status: InvocationStatus = InvocationStatus.queued,
) -> Invocation:
    """Add (and flush) the invocation of a run of the agent or workflow (kind, id) that no
    mention started: a workflow agent node's (`parent`, its workflow run's invocation, whose
    cause it takes), a schedule's (`trigger_id`), else one started by hand by `requested_by`.
    The caller links the run to it; in the caller's transaction."""
    if parent is not None:
        cause = parent.cause
    elif trigger_id is not None:
        cause = InvocationCause.schedule
    else:
        cause = InvocationCause.manual
    is_agent = kind == TriggerTargetKind.agent
    invocation = Invocation(
        cause=cause,
        requested_by=requested_by,
        trigger_id=trigger_id,
        parent_id=None if parent is None else parent.id,
        ancestor_ids=[] if parent is None else [*parent.ancestor_ids, parent.id],
        depth=0 if parent is None else parent.depth + 1,
        target_kind=kind,
        agent_id=target_id if is_agent else None,
        workflow_id=None if is_agent else target_id,
        status=status,
        started_at=func.now() if status == InvocationStatus.running else None,
    )
    store.add(invocation)
    store.flush()
    return invocation


def of_workflow_run(store: Session, workflow_run_id: int) -> Invocation | None:
    """The store's workflow run's invocation."""
    return store.scalars(
        select(Invocation)
        .where(Invocation.workflow_run_id == workflow_run_id)
        .order_by(Invocation.id)
        .limit(1)
    ).one_or_none()


def create(
    store: Session, message: ChatMessage, targets: Sequence[tuple[TriggerTargetKind, int]]
) -> list[Invocation]:
    """Queue an invocation of each agent or workflow (kind, id) the (just added) message
    mentions, each with its acknowledgement and card; in the caller's transaction. Nothing for a
    message that isn't a user's. An archived agent's or workflow's fails at once ("The agent
    <name> is archived"), before any handoff, so no model is called for it."""
    if message.author_kind != ChatAuthorKind.user:
        return []
    store.flush()
    made = []
    targets = list(targets)
    archived = _archived(store, targets)
    for kind, target_id in targets:
        is_agent = kind == TriggerTargetKind.agent
        invocation = Invocation(
            chat_id=message.chat_id,
            message_id=message.id,
            requested_by=message.author_user_id,
            target_kind=kind,
            agent_id=target_id if is_agent else None,
            workflow_id=None if is_agent else target_id,
            status=InvocationStatus.queued,
        )
        store.add(invocation)
        store.flush()
        store.add(
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
        store.flush()
        if (name := archived.get((kind, target_id))) is not None:
            fail_unstarted(store, invocation.id, f"The {kind.value} {name} is archived")
        made.append(invocation)
    return made


def _archived(
    store: Session, targets: Sequence[tuple[TriggerTargetKind, int]]
) -> dict[tuple[TriggerTargetKind, int], str]:
    """The names of these agents and workflows (kind, id) that are archived, by (kind, id)."""
    found: dict[tuple[TriggerTargetKind, int], str] = {}
    for kind, model in ((TriggerTargetKind.agent, Agent), (TriggerTargetKind.workflow, Workflow)):
        ids = [i for k, i in targets if k == kind]
        if not ids:
            continue
        for i, name in store.execute(
            select(model.id, model.name).where(model.id.in_(ids), model.archived_at.is_not(None))
        ):
            found[kind, i] = name
    return found


def lock(store: Session, invocation_id: int) -> Invocation | None:
    """The invocation, locked until commit, if it hasn't finished."""
    return store.scalars(
        select(Invocation)
        .where(Invocation.id == invocation_id, Invocation.status.in_(UNFINISHED))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def begin_briefing(store: Session, invocation_id: int) -> Invocation | None:
    """The queued invocation, locked and now briefing (`aios.handoff`); None if it isn't
    queued. In the caller's transaction."""
    invocation = lock(store, invocation_id)
    if invocation is None or invocation.status != InvocationStatus.queued:
        return None
    invocation.status = InvocationStatus.briefing
    return invocation


def record_handoff(store: Session, invocation_id: int, ack: str, brief: str) -> None:
    """Keep the handoff's brief for the run, and put its ack in place of the acknowledgement's
    placeholder; in the caller's transaction. A no-op unless it's still briefing."""
    invocation = lock(store, invocation_id)
    if invocation is None or invocation.status != InvocationStatus.briefing:
        return
    invocation.brief = brief
    search_index.index_brief(store, invocation)
    if ack:
        store.execute(
            update(ChatMessage)
            .where(
                ChatMessage.invocation_id == invocation_id,
                ChatMessage.kind == ChatMessageKind.run_card,
            )
            .values(body=ack)
        )


def mark_running(store: Session, invocation_id: int) -> None:
    """Its run has been queued: it's running. A no-op if the run has already ended it."""
    store.execute(
        update(Invocation)
        .where(
            Invocation.id == invocation_id,
            Invocation.status.in_(NOT_STARTED),
        )
        .values(
            status=InvocationStatus.running,
            started_at=func.coalesce(Invocation.started_at, func.now()),
        )
    )


def _finish(
    store: Session, invocation: Invocation, status: InvocationStatus, body: str, error=None
) -> None:
    """End the (locked, unfinished) invocation and post its result back into its chat, if a
    chat follows it."""
    invocation.status = status
    invocation.error = error
    invocation.finished_at = func.now()
    if invocation.chat_id is None:
        return
    result = ChatMessage(
        chat_id=invocation.chat_id,
        author_kind=ChatAuthorKind(invocation.target_kind.value),
        author_workflow_id=invocation.workflow_id,
        author_agent_id=invocation.agent_id,
        kind=ChatMessageKind.text,
        body=body,
        invocation_id=invocation.id,
    )
    store.add(result)
    search_index.index_chat_message(store, result)
    # A result is activity.
    store.execute(update(Chat).where(Chat.id == invocation.chat_id).values(updated_at=func.now()))


def failed(store: Session, invocation: Invocation, error: str) -> None:
    _finish(store, invocation, InvocationStatus.failed, f"The run failed: {error}", error)


def fail_unstarted(store: Session, invocation_id: int, error: str) -> None:
    """Fail an invocation whose run couldn't start, with why; in the caller's transaction. A
    no-op once its run has started (it finishes when the run does), or it has finished."""
    invocation = lock(store, invocation_id)
    if invocation is None or invocation.status not in NOT_STARTED:
        return
    _finish(
        store,
        invocation,
        InvocationStatus.failed,
        f"Couldn't start the run: {error}",
        error,
    )


def finish_workflow_runs(store: Session, runs: Sequence[WorkflowRun]) -> None:
    """Finish the invocations of these workflow runs (of the store `store` is a session on),
    which have just ended (as they are now), with how each went; in the caller's transaction."""
    by_id = {run.id: run for run in runs}
    if not by_id:
        return
    invocations = store.scalars(
        select(Invocation)
        .where(
            Invocation.workflow_run_id.in_(list(by_id)),
            Invocation.status.in_(UNFINISHED),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for invocation in invocations:
        run = by_id[invocation.workflow_run_id]
        # Started, even if it ended before it was marked running.
        invocation.started_at = func.coalesce(Invocation.started_at, run.started_at, func.now())
        if run.status == WorkflowRunStatus.succeeded:
            _finish(store, invocation, InvocationStatus.succeeded, run.output or NO_OUTPUT)
        else:
            failed(store, invocation, run.error or "The run failed.")


def fail_deleted_run(store: Session, workflow_run_id: int) -> None:
    """Fail the invocation of the store's workflow run, being deleted before it finished; in the
    caller's transaction, before the run's deletion clears the invocation's link to it."""
    for invocation in store.scalars(
        select(Invocation)
        .where(
            Invocation.workflow_run_id == workflow_run_id,
            Invocation.status.in_(UNFINISHED),
        )
        .with_for_update()
    ):
        failed(store, invocation, RUN_DELETED)


def fail_stalled(store: Session, chat_id: int) -> None:
    """Fail the chat's workflow invocations that never started and never will: still queued or
    briefing without a run long after they were made, their process gone. Commits. (An agent's
    waits for a free slot, however long; see `claim_turns`.)"""
    for invocation in store.scalars(
        select(Invocation)
        .where(
            Invocation.chat_id == chat_id,
            Invocation.target_kind == TriggerTargetKind.workflow,
            Invocation.status.in_(NOT_STARTED),
            Invocation.workflow_run_id.is_(None),
            Invocation.agent_run_id.is_(None),
            Invocation.created_at < func.now() - START_WITHIN,
        )
        .with_for_update(skip_locked=True)
    ):
        failed(store, invocation, NEVER_STARTED)
    store.commit()


def claim_turns(store: Session, limit: int) -> list[int]:
    """Claim up to `limit` of the store's agent invocations waiting for a process to run their
    turn, the oldest first, for this process to run: each gets its heartbeat, which the process
    keeps while it runs the turn. Commits; returns their ids."""
    claimed = store.scalars(
        select(Invocation)
        .where(
            Invocation.target_kind == TriggerTargetKind.agent,
            Invocation.status == InvocationStatus.queued,
            Invocation.heartbeat_at.is_(None),
        )
        .order_by(Invocation.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for invocation in claimed:
        invocation.heartbeat_at = func.now()
    store.commit()
    return [i.id for i in claimed]


def beat(store: Session, invocation_id: int) -> None:
    """Refresh a claimed invocation's heartbeat while its turn runs; in the caller's
    transaction. Not once it has finished."""
    store.execute(
        update(Invocation)
        .where(Invocation.id == invocation_id, Invocation.status.in_(UNFINISHED))
        .values(heartbeat_at=func.now())
    )


def last_reply(data_store_id: uuid.UUID, run_id: int) -> str:
    """The agent's final reply in the store's run: its last message, if that's the model's."""
    history = checkpoints.history(data_store_id, run_id)
    if history and history[-1].type == "ai" and history[-1].text.strip():
        return history[-1].text
    return NO_REPLY


def finish_turn(store: Session, invocation_id: int, error: str | None) -> None:
    """Finish an agent invocation whose turn has ended (`error` None if it went fine): post the
    agent's reply, or why it failed; in the caller's transaction. A no-op if it has already
    finished (failed as stalled, say)."""
    invocation = lock(store, invocation_id)
    if invocation is None:
        return
    if invocation.status in NOT_STARTED:
        # The run never started.
        _finish(
            store,
            invocation,
            InvocationStatus.failed,
            f"Couldn't start the run: {error or NEVER_STARTED}",
            error or NEVER_STARTED,
        )
    elif invocation.agent_run_id is None:
        failed(store, invocation, RUN_DELETED)
    elif error is not None:
        failed(store, invocation, error)
    elif not store.scalar(select(waiting_on_a_request(invocation.agent_run_id))):
        reply = last_reply(store_id_of(store), invocation.agent_run_id)
        _finish(store, invocation, InvocationStatus.succeeded, reply)
    # Otherwise it waits, running, for the run's resume to finish it (`finish_resumed`).


def waiting_on_a_request(run_id):
    """Whether the agent run (an id, or a column of one) waits on a schema change request: not
    yet resumed with its outcome."""
    return (
        select(SchemaChangeRequest.id)
        .where(SchemaChangeRequest.requested_by_run == run_id, *not_given())
        .exists()
    )


def finish_resumed(store: Session, run_id: int, error: str | None) -> None:
    """Finish the agent invocation whose run (the store's) waited on a schema change request,
    now the run has been resumed (`error` None if it went fine), as `finish_turn` does: unless it
    waits on another. In the caller's transaction; nothing for a run no invocation is waiting
    on."""
    invocation_id = store.scalar(
        select(Invocation.id).where(
            Invocation.agent_run_id == run_id,
            Invocation.status == InvocationStatus.running,
        )
    )
    if invocation_id is not None:
        finish_turn(store, invocation_id, error)


def fail_stopped_turns(
    store: Session, *, stale_after: timedelta, error: str, chat_id: int | None = None
) -> None:
    """Fail the store's unfinished agent invocations (the chat's, or all) whose heartbeat has
    stopped for `stale_after`, their process gone, with `error`; and their runs, if still marked
    running. In the caller's transaction."""
    for invocation in store.scalars(
        select(Invocation)
        .where(
            Invocation.target_kind == TriggerTargetKind.agent,
            Invocation.status.in_(UNFINISHED),
            Invocation.heartbeat_at < func.now() - stale_after,
            # No process beats a run waiting for a member's decision.
            ~waiting_on_a_request(Invocation.agent_run_id),
            true() if chat_id is None else Invocation.chat_id == chat_id,
        )
        .with_for_update(skip_locked=True)
    ):
        if invocation.agent_run_id is not None:
            from aios import turns

            turns.fail_running(store, AgentRun.id == invocation.agent_run_id, error=error)
        failed(store, invocation, error)


def finish_quietly(store: Session, run_id: int, error: str | None) -> None:
    """Finish the unfinished invocation no chat follows of the store's agent run, whose turn has
    just ended (`error` None if it went fine): unless the run waits on a schema change request,
    whose resume's turn ends it. In the caller's transaction."""
    invocation = store.scalars(
        select(Invocation)
        .where(
            Invocation.agent_run_id == run_id,
            Invocation.chat_id.is_(None),
            Invocation.status.in_(UNFINISHED),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if invocation is None:
        return
    invocation.started_at = func.coalesce(Invocation.started_at, func.now())
    if error is not None:
        failed(store, invocation, error)
    elif not store.scalar(select(waiting_on_a_request(run_id))):
        _finish(store, invocation, InvocationStatus.succeeded, "")


def fail_deleted_agent_run(store: Session, run_id: int) -> None:
    """Fail the unfinished invocation no chat follows of the store's agent run, being deleted;
    in the caller's transaction, before the run's deletion clears its link to it. (A chat's is
    finished by its turn, `finish_turn`.)"""
    for invocation in store.scalars(
        select(Invocation)
        .where(
            Invocation.agent_run_id == run_id,
            Invocation.chat_id.is_(None),
            Invocation.status.in_(UNFINISHED),
        )
        .with_for_update()
    ):
        failed(store, invocation, RUN_DELETED)
