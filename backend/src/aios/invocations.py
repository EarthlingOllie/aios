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
workflow posts (a result, or an agent's `post_message` post) starts nothing, whatever it
mentions, so a chat can't loop.

Its start is then enqueued on the run service (`aios.run_service`). First the handoff
(`aios.handoff`) briefs it: the invocation goes `briefing` (`begin_briefing`), and one model call
writes the brief, kept on the invocation, and the ack, which replaces the acknowledgement's
placeholder (`record_handoff`). Then the run starts through the helpers the UI and schedules use,
with the brief and the message as its input (`run_input`), started by the member who mentioned
it (`requested_by`). The invocation records the run, and goes `running` once it's started
(`mark_running`). A start that's refused, or a handoff that fails, fails it with the reason
(`fail_unstarted`).

Every invocation ends the same way, in one transaction: it's marked succeeded or failed and its
result is posted back as a message from the agent or workflow, belonging to the invocation
(`_finish`), so whoever is following the card sees it end with the message. A workflow run ends
in the worker or when it's found stopped (`aios.workflows._end_runs`, which calls
`finish_workflow_runs`), or is deleted before it does (`fail_deleted_run`). An invocation whose
start keeps losing its process is failed by the run service after a few attempts.

Mentioning an agent queues an invocation the same way; after the handoff the run service runs
its turn (`aios.run_service.work`), a new turn of the agent's one conversation run in the chat
(started by its first mention there). When the turn ends its reply, or why it failed, is posted
back (`finish_turn`). A turn that ends waiting for a member to decide its schema change request
leaves the invocation running, its card showing the run waiting; the run's resume, once the
request is decided, finishes it (`finish_resumed`).

An agent's mentions in a chat take their turns one at a time, in message order: one made while
an earlier one hasn't finished (its turn is under way, or its run waits on an approval) stays
queued (`queued_behind`; its card says what it waits for, `waiting_for`). When a mention of an
agent finishes, however it ends, the next one queued is started once the store commits
(`_start_next`).

A run an invocation started is the organisation's, like any other: started by the member who
mentioned it, executed for the organisation, and read by every member (`aios.api.agents`).

Chats, their messages, invocations, runs and schema change requests are all in the
organisation's store, so everything here is on a session on it (`store`), in one transaction:
a run deleted clears the invocation's link to it by its foreign key.
"""

import json
import logging
import re
import uuid
from collections.abc import Sequence

from sqlalchemy import event as sa_event
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.orm import Session

from aios import checkpoints, jev, limits, notifications, outbox, search_index, streams
from aios.config import get_settings
from aios.datastores import store_id_of
from aios.datastores.models import (
    Agent,
    Chat,
    ChatMessage,
    Event,
    Invocation,
    SchemaChangeRequest,
    Trigger,
    Workflow,
    WorkflowRun,
)
from aios.datastores.schema_changes import not_given
from aios.db import SessionLocal
from aios.models import (
    ChatAuthorKind,
    ChatMessageKind,
    DataStore,
    InvocationCause,
    InvocationStatus,
    TriggerTargetKind,
    WorkflowRunStatus,
    WorkLane,
)

log = logging.getLogger(__name__)

# The acknowledgement a run card carries until the handoff replaces it with what the member
# understood (`aios.handoff`).
ACKNOWLEDGEMENT = "Reading the chat…"
NEVER_STARTED = "The service stopped before the run could start."
RUN_DELETED = "The run was deleted before it finished."
NO_OUTPUT = "_The run finished with no output._"
NO_FINAL_STEP = "_No final step ran._"
NO_REPLY = "_The agent finished without a reply._"
# Why a mention of an archived agent or workflow is refused (as `archived_refusal` says).
ARCHIVED = "The {kind} {name} is archived."

# Before its run has started, and before it has finished.
NOT_STARTED = (InvocationStatus.queued, InvocationStatus.briefing)
UNFINISHED = (*NOT_STARTED, InvocationStatus.running)
# Not finished yet: its run going, or its result being handed back (`aios.handback`).
LIVE = (*UNFINISHED, InvocationStatus.handing_back)

# A mention's token in a message (`@[name](kind:id)`, the name with `\`, `[` and `]` escaped).
TOKEN = re.compile(r"@\[((?:\\.|[^\\\]])*)\]\((?:user|agent|workflow):[1-9]\d*\)")


def message_text(body: str) -> str:
    """A message's text, with each mention as `@name`."""
    return TOKEN.sub(lambda m: "@" + re.sub(r"\\(.)", r"\1", m.group(1)), body)


# Where a person's chat message says it's from, in its untrusted block.
CHAT_MESSAGE = "chat message"


def run_input(brief: str | None, body: str) -> str:
    """A run's input: the handoff's brief, a blank line, then the message's text (just the
    text if the chat had nothing to brief it on). Each is wrapped as untrusted (`aios.jev.wrap`),
    whatever Jev said of the message: the message is a person's, which Jev may have misjudged,
    and the brief is derived text, written by the handoff's model from the chat."""
    text = jev.wrap(message_text(body), CHAT_MESSAGE)
    return f"{jev.wrap(brief, 'brief')}\n\n{text}" if brief else text


def for_run(
    store: Session,
    kind: TriggerTargetKind,
    target_id: int,
    *,
    requested_by: int | None,
    trigger_id: int | None = None,
    parent: Invocation | None = None,
    event: Event | None = None,
    started_by: Invocation | None = None,
    status: InvocationStatus = InvocationStatus.queued,
) -> Invocation:
    """Add (and flush) the invocation of a run of the agent or workflow (kind, id) that no
    mention started: a workflow agent node's (`parent`, its workflow run's invocation, whose
    cause it takes), an event trigger's (`trigger_id` and the `event` it fired on, whose chain
    of causes it continues), an agent's `start_workflow` call's (`started_by`, the calling
    turn's invocation, whose chain it continues), a schedule's (`trigger_id`), else one started
    by hand by `requested_by`. The caller links the run to it; in the caller's transaction."""
    if parent is not None:
        cause = parent.cause
        ancestors = [*parent.ancestor_ids, parent.id]
    elif started_by is not None:
        cause = InvocationCause.start_workflow
        ancestors = [*started_by.ancestor_ids, started_by.id]
    elif event is not None:
        cause = InvocationCause.event
        ancestors = list(event.chain)
    elif trigger_id is not None:
        cause = InvocationCause.schedule
        ancestors = []
    else:
        cause = InvocationCause.manual
        ancestors = []
    is_agent = kind == TriggerTargetKind.agent
    # A trigger's run delivers its result to the chat the trigger names, as it fires.
    deliver_to = None
    if trigger_id is not None and parent is None:
        deliver_to = store.scalar(
            select(Trigger.destination_chat_id).where(Trigger.id == trigger_id)
        )
    invocation = Invocation(
        cause=cause,
        requested_by=requested_by,
        trigger_id=trigger_id,
        parent_id=None if parent is None else parent.id,
        event_id=None if event is None else event.id,
        ancestor_ids=ancestors,
        depth=len(ancestors),
        target_kind=kind,
        agent_id=target_id if is_agent else None,
        workflow_id=None if is_agent else target_id,
        status=status,
        started_at=func.now() if status == InvocationStatus.running else None,
        delivery_chat_id=deliver_to,
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
    store: Session,
    message: ChatMessage,
    targets: Sequence[tuple[TriggerTargetKind, int]],
    refused: dict[tuple[TriggerTargetKind, int], str] | None = None,
) -> list[Invocation]:
    """Queue an invocation of each agent or workflow (kind, id) the (just added) message
    mentions, each with its acknowledgement and card; in the caller's transaction. Nothing for a
    message that isn't a user's. One in `refused` (the mention guards', `aios.guards`) is
    refused at once with the reason, which its card says; so is an archived agent's or
    workflow's ("The agent <name> is archived."). Either is before any handoff, so no model is
    called for it."""
    refused = refused or {}
    if message.author_kind != ChatAuthorKind.user:
        return []
    store.flush()
    made = []
    targets = list(targets)
    archived = _archived(store, targets)
    for kind, target_id in targets:
        is_agent = kind == TriggerTargetKind.agent
        reason = refused.get((kind, target_id))
        if reason is None and (name := archived.get((kind, target_id))) is not None:
            reason = ARCHIVED.format(kind=kind.value, name=name)
        invocation = Invocation(
            chat_id=message.chat_id,
            message_id=message.id,
            requested_by=message.author_user_id,
            target_kind=kind,
            agent_id=target_id if is_agent else None,
            workflow_id=None if is_agent else target_id,
            status=InvocationStatus.queued if reason is None else InvocationStatus.refused,
            refusal_reason=reason,
            finished_at=None if reason is None else func.now(),
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
                body=ACKNOWLEDGEMENT if reason is None else reason,
                invocation_id=invocation.id,
            )
        )
        store.flush()
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


def _earlier_unfinished(invocation: Invocation):
    """The unfinished mentions of the invocation's agent in its chat made before it."""
    return select(Invocation.id).where(
        Invocation.chat_id == invocation.chat_id,
        Invocation.agent_id == invocation.agent_id,
        Invocation.id < invocation.id,
        Invocation.status.in_(LIVE),
    )


def queued_behind(store: Session, invocation: Invocation) -> bool:
    """Whether a mention of an agent waits its turn: an earlier mention of the agent in the
    chat hasn't finished (its turn is under way, its run waits on an approval, or it waits
    itself)."""
    if invocation.chat_id is None or invocation.agent_id is None:
        return False
    return bool(store.scalar(select(_earlier_unfinished(invocation).exists())))


# What a queued mention's card says it waits for.
WAITING_FOR_APPROVAL = "approval"
WAITING_FOR_TURN = "turn"


def waiting_for(store: Session, queued: Sequence[tuple[int, int, int]]) -> dict[int, str]:
    """What each of these mentions of agents not started yet, as (id, chat id, agent id), waits
    for, by id: `approval` while the agent's conversation run in the chat waits on one, else
    `turn` while an earlier mention of the agent there hasn't finished. Those that wait for
    nothing are left out."""
    from aios.datastores.models import AgentRun

    if not queued:
        return {}
    pairs = list({(chat_id, agent_id) for _, chat_id, agent_id in queued})
    # The oldest unfinished mention of each agent in each chat: the others wait behind it.
    heads = {
        (chat_id, agent_id): head
        for chat_id, agent_id, head in store.execute(
            select(Invocation.chat_id, Invocation.agent_id, func.min(Invocation.id))
            .where(
                tuple_(Invocation.chat_id, Invocation.agent_id).in_(pairs),
                Invocation.status.in_(LIVE),
            )
            .group_by(Invocation.chat_id, Invocation.agent_id)
        )
    }
    approval = {
        (chat_id, agent_id)
        for chat_id, agent_id in store.execute(
            select(AgentRun.chat_id, AgentRun.agent_id).where(
                tuple_(AgentRun.chat_id, AgentRun.agent_id).in_(pairs),
                waiting_on_a_request(AgentRun.id),
            )
        )
    }
    out = {}
    for invocation_id, chat_id, agent_id in queued:
        head = heads.get((chat_id, agent_id))
        if head is None or head >= invocation_id:
            continue
        waits = (chat_id, agent_id) in approval
        out[invocation_id] = WAITING_FOR_APPROVAL if waits else WAITING_FOR_TURN
    return out


_NEXT = "aios.invocations.next"


def _start_next(store: Session, invocation: Invocation) -> None:
    """The invocation, a mention of an agent, has just finished: once the store commits, start
    the next mention of the agent queued in the chat behind it, if there is one."""
    if invocation.chat_id is None or invocation.agent_id is None:
        return
    following = store.scalar(
        select(Invocation.id)
        .where(
            Invocation.chat_id == invocation.chat_id,
            Invocation.agent_id == invocation.agent_id,
            Invocation.id > invocation.id,
            Invocation.status.in_(NOT_STARTED),
        )
        .order_by(Invocation.id)
        .limit(1)
    )
    if following is not None:
        store.info.setdefault(_NEXT, set()).add(following)


def enqueue_next(data_store_id: uuid.UUID, invocation_ids: Sequence[int]) -> None:
    """Enqueue the starts of the store's queued mentions whose turn has come, and wake the run
    service for them: each runs again if it's running now (having found it still had to wait).
    One whose enqueue is lost is put back by the requeue sweep."""
    from aios import run_service
    from aios.run_service import work

    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        if organisation_id is None:
            return
        run_service.enqueue(
            db,
            [
                work.start_work(organisation_id, data_store_id, i, again_if_running=True)
                for i in invocation_ids
            ],
        )
        db.commit()
    run_service.wake(WorkLane.interactive)


@sa_event.listens_for(Session, "after_commit")
def _enqueue_next(session: Session) -> None:
    due: set[int] | None = session.info.pop(_NEXT, None)
    if not due:
        return
    try:
        enqueue_next(store_id_of(session), sorted(due))
    except Exception:
        # The requeue sweep finds them (`aios.run_service.work.lost`).
        log.exception("couldn't enqueue the next mentions")


@sa_event.listens_for(Session, "after_rollback")
def _forget_next(session: Session) -> None:
    session.info.pop(_NEXT, None)


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
    queued. One left briefing with no brief (its handoff's process stopped part way) is
    briefed again. In the caller's transaction."""
    invocation = lock(store, invocation_id)
    if invocation is None:
        return None
    if invocation.status == InvocationStatus.briefing and invocation.brief is None:
        return invocation
    if invocation.status != InvocationStatus.queued:
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
    streams.invocation_changed(store, invocation_id)


def _finish(
    store: Session, invocation: Invocation, status: InvocationStatus, body: str, error=None
) -> None:
    """End the (locked, unfinished) invocation and post its result back into its chat, if a
    chat follows it: as written, or handed back to be summarised first (`hands_back`). A
    trigger's run that delivers to a chat posts there instead (`_deliver`)."""
    invocation.status = status
    invocation.error = error
    invocation.finished_at = func.now()
    if invocation.chat_id is None:
        if invocation.delivery_chat_id is not None and invocation.parent_id is None:
            _deliver(store, invocation, status, body, error)
            return
        _notify(store, invocation, status, None)
        return
    if status == InvocationStatus.succeeded and hands_back(invocation, body):
        # The next mention waits for the result, posted by the handback (`post_handback`).
        _hand_back(store, invocation, body)
        return
    _start_next(store, invocation)
    result = _post(store, invocation, invocation.chat_id, body)
    _notify(store, invocation, status, result)


def _post(store: Session, invocation: Invocation, chat_id: int, body: str) -> ChatMessage:
    """Post the invocation's result into the chat, as a message from its agent or workflow
    belonging to it: derived text (its verdict `derived`), never re-classified."""
    result = ChatMessage(
        chat_id=chat_id,
        author_kind=ChatAuthorKind(invocation.target_kind.value),
        author_workflow_id=invocation.workflow_id,
        author_agent_id=invocation.agent_id,
        kind=ChatMessageKind.text,
        body=body,
        invocation_id=invocation.id,
        verdict=jev.Verdict.derived,
    )
    store.add(result)
    search_index.index_chat_message(store, result)
    outbox.message_posted(store, result, invocation_id=invocation.id)
    # A result is activity.
    store.execute(update(Chat).where(Chat.id == chat_id).values(updated_at=func.now()))
    return result


# Results that say there's nothing to summarise.
PLACEHOLDERS = (NO_OUTPUT, NO_FINAL_STEP, NO_REPLY)


def is_json(text: str) -> bool:
    """Whether the text is a JSON object or array."""
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def hands_back(invocation: Invocation, body: str) -> bool:
    """Whether a mention's successful result is summarised by the handback before it's posted
    (`aios.handback`): a workflow's, and an agent's longer than `handback_threshold` characters
    or in JSON. An agent's short reply is posted as written."""
    if invocation.parent_id is not None or invocation.cause != InvocationCause.mention:
        return False
    if body in PLACEHOLDERS or not body.strip():
        return False
    if invocation.target_kind == TriggerTargetKind.workflow:
        return True
    return len(body) > get_settings().handback_threshold or is_json(body)


_HANDBACKS = "aios.invocations.handbacks"


def _hand_back(store: Session, invocation: Invocation, output: str) -> None:
    """Keep the run's full output and mark the invocation handing back, its card showing
    "Summarising"; once the store commits, its handback is enqueued (`aios.handback`)."""
    invocation.status = InvocationStatus.handing_back
    invocation.finished_at = None
    invocation.full_output = output
    store.info.setdefault(_HANDBACKS, set()).add(invocation.id)
    streams.invocation_changed(store, invocation.id)


def enqueue_handbacks(data_store_id: uuid.UUID, invocation_ids: Sequence[int]) -> None:
    """Enqueue the handbacks of the store's invocations, committed handing back, and wake the
    run service for them."""
    from aios.run_service import queue, work

    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        if organisation_id is None:
            return
        queue.enqueue(
            db, [work.handback_work(organisation_id, data_store_id, i) for i in invocation_ids]
        )
        db.commit()
    queue.wake(WorkLane.interactive)


@sa_event.listens_for(Session, "after_commit")
def _enqueue_handbacks(session: Session) -> None:
    due: set[int] | None = session.info.pop(_HANDBACKS, None)
    if not due:
        return
    try:
        enqueue_handbacks(store_id_of(session), sorted(due))
    except Exception:
        # The requeue sweep finds them (`aios.run_service.work.lost`).
        log.exception("couldn't enqueue handbacks")


@sa_event.listens_for(Session, "after_rollback")
def _forget_handbacks(session: Session) -> None:
    session.info.pop(_HANDBACKS, None)


def post_handback(
    store: Session,
    invocation_id: int,
    body: str,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Post what the handback gave for the invocation handing back (the summary, or the raw
    output with a note if summarising failed, `error`) as its result, and mark it succeeded;
    in the caller's transaction. A no-op unless it's handing back. A delivery to a chat that has
    been archived since posts nothing, and says why."""
    invocation = store.scalars(
        select(Invocation)
        .where(Invocation.id == invocation_id, Invocation.status == InvocationStatus.handing_back)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if invocation is None:
        return
    invocation.status = InvocationStatus.succeeded
    invocation.finished_at = func.now()
    invocation.summary = summary
    invocation.handback_error = error
    _start_next(store, invocation)
    chat_id = invocation.chat_id
    if chat_id is None:
        chat = _destination(store, invocation)
        if chat is None:
            return
        chat_id = chat.id
    result = _post(store, invocation, chat_id, body)
    _notify(store, invocation, InvocationStatus.succeeded, result)


# --- Delivering a trigger's results to a chat ----------------------------------------------------

UNDELIVERED = "{chat} is archived, so the result wasn't delivered. Choose another chat."
UNDELIVERED_GONE = "The chat has gone, so the result wasn't delivered. Choose another chat."
# How much of why a delivered run failed its failure message says.
FAILURE_LIMIT = 500


def _destination(store: Session, invocation: Invocation) -> Chat | None:
    """The chat the trigger's run delivers to, if it can still be posted in; else None, with why
    on the invocation (`delivery_error`), and whoever chose the chat notified."""
    chat = store.get(Chat, invocation.delivery_chat_id) if invocation.delivery_chat_id else None
    if chat is not None and chat.archived_at is None:
        return chat
    invocation.delivery_error = (
        UNDELIVERED_GONE if chat is None else UNDELIVERED.format(chat=chat.name)
    )
    setter = None
    if invocation.trigger_id is not None:
        setter = store.scalar(
            select(Trigger.destination_set_by).where(Trigger.id == invocation.trigger_id)
        )
    ref = _run_ref(invocation)
    if setter is not None and ref is not None:
        notifications.after_store_commit(
            store, [setter], notifications.RUN_FAILED, {**ref, "undelivered": True}
        )
    return None


def _deliver(
    store: Session, invocation: Invocation, status: InvocationStatus, body: str, error=None
) -> None:
    """End a trigger's run that delivers to a chat: once it has run, its result handed back to
    be summarised and posted there, or a short failure message posted. Nothing is posted in an
    archived chat (`_destination`); the run has still run."""
    chat = _destination(store, invocation)
    if chat is None:
        _notify(store, invocation, status, None)
        return
    if status == InvocationStatus.succeeded:
        output = body
        if not output and invocation.agent_run_id is not None:
            output = last_reply(store_id_of(store), invocation.agent_run_id)
        _hand_back(store, invocation, output or NO_OUTPUT)
        return
    reason = error or "The run failed."
    if len(reason) > FAILURE_LIMIT:
        reason = reason[: FAILURE_LIMIT - 1].rstrip() + "…"
    result = _post(store, invocation, chat.id, f"The run failed: {reason}")
    _notify(store, invocation, status, result)


# The causes whose runs' ends notify whoever caused them.
NOTIFYING = (InvocationCause.mention, InvocationCause.manual)
# The causes whose runs' failures notify their trigger's creator and the admins.
TRIGGERED = (InvocationCause.schedule, InvocationCause.event)


def _run_ref(invocation: Invocation) -> dict | None:
    """What a notification about the invocation's run names: the agent or workflow, and run."""
    if invocation.agent_run_id is not None:
        return {"agent_id": invocation.agent_id, "run_id": invocation.agent_run_id}
    if invocation.workflow_run_id is not None:
        return {"workflow_id": invocation.workflow_id, "run_id": invocation.workflow_run_id}
    return None


def _notify(
    store: Session, invocation: Invocation, status: InvocationStatus, result: ChatMessage | None
) -> None:
    """Once the store commits the end of an invocation a person caused (a mention, or starting
    its run by hand; not a workflow's agent node's, whose workflow run's own says), notify them
    (`aios.notifications`): a failure as `run_failed`, about the result posted or else the run;
    a mention's result as `reply`, unless they're watching the chat. A trigger's run that failed
    notifies the trigger's creator and the organisation's admins."""
    if invocation.parent_id is not None:
        return
    failed = status == InvocationStatus.failed
    if invocation.cause in TRIGGERED:
        if not failed or invocation.trigger_id is None:
            return
        if result is not None:
            store.flush()
            ref = notifications.chat_ref(result.chat_id, result.id)
        else:
            ref = _run_ref(invocation)
        creator = store.scalar(
            select(Trigger.created_by).where(Trigger.id == invocation.trigger_id)
        )
        if ref is not None:
            notifications.after_store_commit(
                store,
                [creator] if creator is not None else [],
                notifications.RUN_FAILED,
                ref,
                and_admins=True,
            )
        return
    if invocation.requested_by is None or invocation.cause not in NOTIFYING:
        return
    if result is not None:
        store.flush()
        ref = notifications.chat_ref(invocation.chat_id, result.id)
        if failed:
            notifications.after_store_commit(
                store, [invocation.requested_by], notifications.RUN_FAILED, ref
            )
        elif invocation.cause == InvocationCause.mention:
            notifications.after_store_commit(
                store,
                [invocation.requested_by],
                notifications.REPLY,
                ref,
                unless_watching=invocation.chat_id,
            )
        return
    if not failed:
        return
    ref = _run_ref(invocation)
    if ref is None:
        return
    notifications.after_store_commit(
        store, [invocation.requested_by], notifications.RUN_FAILED, ref
    )


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
            # A run in which no final step ran (its branches were all skipped) has no output.
            result = NO_FINAL_STEP if run.output is None else run.output or NO_OUTPUT
            _finish(store, invocation, InvocationStatus.succeeded, result)
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


def last_reply(data_store_id: uuid.UUID, run_id: int) -> str:
    """The agent's final reply in the store's run: its last message, if that's the model's."""
    history = checkpoints.history(data_store_id, run_id)
    if history and history[-1].type == "ai" and history[-1].text.strip():
        return history[-1].text
    return NO_REPLY


def partial_reply(data_store_id: uuid.UUID, run_id: int) -> str:
    """What the agent said in the store's run's last turn, up to where it stopped: its messages'
    text since the last human message, in order; empty if none."""
    history = checkpoints.history(data_store_id, run_id)
    said: list[str] = []
    for message in reversed(history):
        if message.type == "human":
            break
        if message.type == "ai" and message.text.strip():
            said.insert(0, message.text.strip())
    return "\n\n".join(said)


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
    elif error is not None and error.startswith(limits.REACHED):
        # Stopped at a cap: what the agent got to, and which limit stopped it.
        partial = partial_reply(store_id_of(store), invocation.agent_run_id)
        body = f"{partial}\n\n_{error}_" if partial else f"_{error}_"
        _finish(store, invocation, InvocationStatus.failed, body, error)
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
    """Fail the unfinished invocations of the store's agent run, being deleted; in the caller's
    transaction, before the run's deletion clears their link to it. A mention's whose turn has
    ended waiting on an approval (cancelled with the run) fails too, so the mentions queued
    behind it in the chat run; one whose turn is under way is failed here, and its turn's end
    finds it finished."""
    for invocation in store.scalars(
        select(Invocation)
        .where(Invocation.agent_run_id == run_id, Invocation.status.in_(UNFINISHED))
        .order_by(Invocation.id)
        .with_for_update()
    ):
        failed(store, invocation, RUN_DELETED)
