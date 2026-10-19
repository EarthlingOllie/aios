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
heartbeat has stopped lost its process, and is failed (`fail_stopped_turns`).

A run an invocation started is the organisation's, like any other: started by the member who
mentioned it, executed for the organisation, and read by every member (`aios.api.agents`).
"""

import re
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session

from aios import checkpoints
from aios.models import (
    AgentRun,
    AgentRunStatus,
    Chat,
    ChatAuthorKind,
    ChatInvocation,
    ChatInvocationStatus,
    ChatMessage,
    ChatMessageKind,
    TriggerTargetKind,
    WorkflowRun,
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
    message that isn't a user's."""
    if message.author_kind != ChatAuthorKind.user:
        return []
    db.flush()
    made = []
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
        made.append(invocation)
    return made


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


def finish_workflow_runs(db: Session, run_ids: Sequence[int]) -> None:
    """Finish the invocations of these workflow runs, which have just ended, with how each went;
    in the caller's transaction."""
    rows = db.execute(
        select(ChatInvocation, WorkflowRun)
        .join(WorkflowRun, WorkflowRun.id == ChatInvocation.workflow_run_id)
        .where(
            ChatInvocation.workflow_run_id.in_(run_ids),
            ChatInvocation.status.in_(UNFINISHED),
        )
        .with_for_update(of=ChatInvocation)
        .execution_options(populate_existing=True)
    ).all()
    for invocation, run in rows:
        # Started, even if it ended before it was marked running.
        invocation.started_at = func.coalesce(ChatInvocation.started_at, run.started_at, func.now())
        if run.status == WorkflowRunStatus.succeeded:
            _finish(db, invocation, ChatInvocationStatus.succeeded, run.output or NO_OUTPUT)
        else:
            failed(db, invocation, run.error or "The run failed.")


def fail_deleted_run(db: Session, workflow_run_id: int) -> None:
    """Fail the invocation of a workflow run being deleted before it finished; in the caller's
    transaction, before the run goes."""
    for invocation in db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.workflow_run_id == workflow_run_id,
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


def last_reply(run_id: int) -> str:
    """The agent's final reply in the run: its last message, if that's the model's."""
    history = checkpoints.history(run_id)
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
        _finish(db, invocation, ChatInvocationStatus.succeeded, last_reply(invocation.agent_run_id))


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
            true() if chat_id is None else ChatInvocation.chat_id == chat_id,
        )
        .with_for_update(skip_locked=True)
    ):
        if invocation.agent_run_id is not None:
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == invocation.agent_run_id,
                    AgentRun.status == AgentRunStatus.running,
                )
                .values(status=AgentRunStatus.failed, finished_at=func.now())
            )
        failed(db, invocation, error)
