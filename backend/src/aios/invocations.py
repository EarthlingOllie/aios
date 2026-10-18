"""What a mention in a chat starts: an invocation of an agent or workflow (`ChatInvocation`).

Posting a message that mentions a workflow member creates, in the message's own transaction, a
queued invocation and the workflow's acknowledgement: a run card (`create`), so the chat never
shows a mention without its card. Only a user's message does this: what an agent or workflow
posts (a result) starts nothing, whatever it mentions, so a chat can't loop.

After the response, a background step starts the run (`aios.api.chats.InvocationFiring`) through
the helpers the UI and schedules use, with the message as its input (`run_input`), as the
workflow's owner. The invocation records the run, and goes `running` once it's queued
(`mark_running`). A start that's refused fails it with the reason (`fail_unstarted`).

Every invocation ends the same way, in one transaction: it's marked succeeded or failed and its
result is posted back as a message from the agent or workflow, belonging to the invocation
(`_finish`), so whoever is following the card sees it end with the message. A workflow run ends
in the worker or when it's found stopped (`aios.workflows._end_runs`, which calls
`finish_workflow_runs`), or is deleted before it does (`run_deleted`). An invocation that never
got as far as starting, because its process stopped first, is failed when the chat is next read
(`fail_stalled`).
"""

import re
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios.models import (
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

# The acknowledgement a run card carries.
ACKNOWLEDGEMENT = "Starting a run with this message."
# How long an invocation can wait to start before it counts as lost with its process: the start
# runs straight after the message is posted and takes moments.
START_WITHIN = timedelta(seconds=90)
NEVER_STARTED = "The service stopped before the run could start."
RUN_DELETED = "The run was deleted before it finished."
NO_OUTPUT = "_The run finished with no output._"

UNFINISHED = (ChatInvocationStatus.queued, ChatInvocationStatus.running)

# A mention's token in a message (`@[name](kind:id)`, the name with `\`, `[` and `]` escaped).
TOKEN = re.compile(r"@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):[1-9]\d*\)")


def run_input(body: str) -> str:
    """A message as a run's input: its text, with each mention as `@name`."""
    return TOKEN.sub(lambda m: "@" + re.sub(r"\\(.)", r"\1", m.group(1)), body)


def create(db: Session, message: ChatMessage, workflow_ids: Sequence[int]) -> list[ChatInvocation]:
    """Queue an invocation of each workflow the (just added) message mentions, each with its
    acknowledgement and card; in the caller's transaction. Nothing for a message that isn't a
    user's."""
    if message.author_kind != ChatAuthorKind.user:
        return []
    db.flush()
    made = []
    for workflow_id in workflow_ids:
        invocation = ChatInvocation(
            chat_id=message.chat_id,
            message_id=message.id,
            requested_by=message.author_user_id,
            target_kind=TriggerTargetKind.workflow,
            workflow_id=workflow_id,
            status=ChatInvocationStatus.queued,
        )
        db.add(invocation)
        db.flush()
        db.add(
            ChatMessage(
                chat_id=message.chat_id,
                author_kind=ChatAuthorKind.workflow,
                author_workflow_id=workflow_id,
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


def mark_running(db: Session, invocation_id: int) -> None:
    """Its run has been queued: it's running. A no-op if the run has already ended it."""
    db.execute(
        update(ChatInvocation)
        .where(
            ChatInvocation.id == invocation_id,
            ChatInvocation.status == ChatInvocationStatus.queued,
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
    if invocation is None or invocation.status != ChatInvocationStatus.queued:
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


def run_deleted(db: Session, workflow_run_id: int) -> None:
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
    """Fail the chat's invocations that never started and never will: still queued without a
    run long after they were made, their process gone. Commits."""
    for invocation in db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.chat_id == chat_id,
            ChatInvocation.status == ChatInvocationStatus.queued,
            ChatInvocation.workflow_run_id.is_(None),
            ChatInvocation.agent_run_id.is_(None),
            ChatInvocation.created_at < func.now() - START_WITHIN,
        )
        .with_for_update(skip_locked=True)
    ):
        failed(db, invocation, NEVER_STARTED)
    db.commit()
