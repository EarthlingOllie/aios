"""A chat's messages: reading them, keeping track of what's been read, and posting."""

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from aios import (
    guards,
    invocations,
    jev,
    notifications,
    organisations,
    outbox,
    run_service,
    search_index,
    turns,
)

# Read on the package at call time, where tests replace them.
from aios.api import chats as package
from aios.api import workflows as workflows_api
from aios.api.chats import rules
from aios.api.chats.common import (
    TARGET_MODELS,
    get_visible_chat,
    is_member,
    lock_chat,
    user_member,
    users_by_id,
)
from aios.api.chats.members import join, mentionables
from aios.api.chats.rules import ChatAction
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.config import get_settings
from aios.datastores import store_id_of
from aios.datastores.models import (
    Agent,
    AgentRun,
    Approval,
    Chat,
    ChatMember,
    ChatMessage,
    Invocation,
    Workflow,
    WorkflowRun,
)
from aios.datastores.schema_changes import not_given
from aios.db import DB, DbId
from aios.models import (
    AgentRunStatus,
    ChatAuthorKind,
    ChatMemberKind,
    ChatMessageKind,
    InvocationStatus,
    TriggerTargetKind,
    WorkLane,
)
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api")


# How many messages a read returns at most: the latest page, a page of history, or what's new.
MESSAGE_PAGE = 50
NOT_MENTIONABLE = "Only the chat's members can be mentioned in it."
NOT_INCLUDED = "This chat doesn't include {names}. Add it to this chat to mention it."
FLAGGED = "Jev flagged this message as a likely prompt injection, so it wasn't posted."
UNCHECKED = "Couldn't check this message, try again."
# The source Jev is told a person's chat message comes from.
SOURCE = "chat message"


class MentionIn(BaseModel):
    kind: ChatMemberKind
    # The user's, agent's or workflow's id, as `GET /api/chats/{id}/mentionable` gives it.
    id: DbId


class MessageIn(BaseModel):
    # Markdown. A mention is written `@[name](kind:id)` in it, which the UI shows as a chip.
    body: str = Field(min_length=1, max_length=100_000, pattern=r"\S")
    # Who the message mentions, chosen from the chat's members (a 422 for anyone else).
    mentions: list[MentionIn] = Field(default=[], max_length=100)


class MentionOut(BaseModel):
    kind: ChatMemberKind
    id: int
    # As it was when the message was posted.
    name: str


class MessageAuthorOut(BaseModel):
    kind: ChatAuthorKind
    # The user's, agent's or workflow's id.
    id: int
    # A user's full name ("First Last").
    name: str


class MessageOut(BaseModel):
    id: int
    chat_id: int
    # Null for a system message, or once the author has been deleted.
    author: MessageAuthorOut | None
    kind: ChatMessageKind
    # Markdown.
    body: str
    mentions: list[MentionOut]
    # The invocation it belongs to: a run card's, or the result its run posted back.
    invocation_id: int | None
    created_at: datetime


class AgentRunLiveOut(BaseModel):
    """How an agent run is going, as its card shows it: the card ticks its elapsed time from
    `started_at` itself."""

    id: int
    status: AgentRunStatus | None
    started_at: datetime | None
    finished_at: datetime | None
    tool_call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    # Whether its turn ended waiting for a member to decide its schema change request.
    waiting: bool


class InvocationOut(BaseModel):
    """A run a mention started, as its card follows it; or a trigger's run whose result was
    delivered to the chat (`delivered`), its card collapsed under the result."""

    id: int
    # The chat its card is in: the mention's, or the one it delivered to.
    chat_id: int
    # The message that mentioned it (null for a delivery), and who posted it (null once
    # they're deleted).
    message_id: int | None
    delivered: bool
    requested_by: int | None
    target_kind: TriggerTargetKind
    # The agent's or workflow's id.
    target_id: int
    status: InvocationStatus
    # Why it failed: the run's error, or why it couldn't start.
    error: str | None
    # Why the mention guards refused it (`aios.guards`): who may invoke the target.
    refusal_reason: str | None
    # The acknowledgement's text as it is now: its placeholder until the handoff writes what the
    # member understood it's being asked to do (the run card's message has the same).
    acknowledgement: str
    # What the handoff told the run of the chat, before the message; null until it has.
    brief: str | None
    # The handback (`aios.handback`): the run's full output, kept when its result was
    # summarised (null if it was posted as written), the summary posted, and why summarising
    # failed (the raw output was posted instead). Its status is `handing_back` meanwhile.
    full_output: str | None
    summary: str | None
    handback_error: str | None
    # Why a delivery to a chat didn't happen.
    delivery_error: str | None
    workflow_run_id: int | None
    agent_run_id: int | None
    # Whether it started a run that has since been deleted.
    run_deleted: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # The workflow run as its page shows it (graph, steps and all); null without one.
    workflow_run: workflows_api.WorkflowRunOut | None
    # The agent run's status, times and counters so far, for this invocation's turns (a
    # conversation run has one or more for each mention); null without one.
    agent_run: AgentRunLiveOut | None
    # A mention of an agent queued behind an earlier one in the chat (`aios.invocations`):
    # `approval` while the agent's run there waits on one, `turn` while the earlier mention's
    # turn is under way or waits itself. Null once it's its turn.
    waiting_for: Literal["approval", "turn"] | None = None


class MessagesOut(BaseModel):
    """Messages of a chat, oldest first, and `invocations`: the state of each invocation they
    belong to and of every one in the chat that hasn't finished, oldest first."""

    messages: list[MessageOut]
    invocations: list[InvocationOut]


# The names of the people, agents and workflows that posted messages, by (kind, id).
AuthorNames = dict[tuple[ChatAuthorKind, int], str]


def author_names(db: Session, store: Session, messages: Sequence[ChatMessage]) -> AuthorNames:
    """The names of who posted these messages: people from the platform, agents and workflows
    from the store (the messages name them by their ids there)."""
    names: AuthorNames = {}
    for kind, model, ids in (
        (ChatAuthorKind.agent, Agent, {m.author_agent_id for m in messages}),
        (ChatAuthorKind.workflow, Workflow, {m.author_workflow_id for m in messages}),
    ):
        ids.discard(None)
        if ids:
            for i, name in store.execute(select(model.id, model.name).where(model.id.in_(ids))):
                names[kind, i] = name
    for i, user in users_by_id(db, [m.author_user_id for m in messages]).items():
        names[ChatAuthorKind.user, i] = user.full_name
    return names


def author_out(message: ChatMessage, names: AuthorNames) -> MessageAuthorOut | None:
    """Who posted the message: None for a system message, or a person who no longer exists."""
    author_id = message.author_user_id or message.author_agent_id or message.author_workflow_id
    if author_id is None:
        return None
    name = names.get((message.author_kind, author_id))
    if name is None and message.author_kind == ChatAuthorKind.user:
        return None
    return MessageAuthorOut(kind=message.author_kind, id=author_id, name=name or "")


def message_out(message: ChatMessage, names: AuthorNames) -> MessageOut:
    return MessageOut(
        id=message.id,
        chat_id=message.chat_id,
        author=author_out(message, names),
        kind=message.kind,
        body=message.body,
        mentions=message.mentions,
        invocation_id=message.invocation_id,
        created_at=message.created_at,
    )


def invocation_out(invocation: Invocation, acknowledgement: str) -> InvocationOut:
    """The invocation, with its acknowledgement's text, without its run (`with_runs` adds it)."""
    run_id = invocation.workflow_run_id or invocation.agent_run_id
    return InvocationOut(
        id=invocation.id,
        chat_id=invocation.chat_id or invocation.delivery_chat_id,
        message_id=invocation.message_id,
        delivered=invocation.chat_id is None,
        requested_by=invocation.requested_by,
        target_kind=invocation.target_kind,
        target_id=invocation.workflow_id or invocation.agent_id,
        status=invocation.status,
        error=invocation.error,
        refusal_reason=invocation.refusal_reason,
        acknowledgement=acknowledgement,
        brief=invocation.brief,
        full_output=invocation.full_output,
        summary=invocation.summary,
        handback_error=invocation.handback_error,
        delivery_error=invocation.delivery_error,
        workflow_run_id=invocation.workflow_run_id,
        agent_run_id=invocation.agent_run_id,
        run_deleted=invocation.started_at is not None and run_id is None,
        created_at=invocation.created_at,
        started_at=invocation.started_at,
        finished_at=invocation.finished_at,
        workflow_run=None,
        agent_run=None,
    )


def agent_runs_live(store: Session, runs: Sequence[tuple[int, int]]) -> dict[int, AgentRunLiveOut]:
    """How each of these agent runs (in the store `store` is a session on) is going for an
    invocation, given as (invocation id, run id): by invocation id, its status, times and
    counters over the invocation's turns of the run, and whether its turn ended waiting on an
    approval (only the invocation that's still running waits). Without a query when there are
    none."""
    if not runs:
        return {}
    run_ids = list({run_id for _, run_id in runs})
    rows = {
        row.invocation_id: row
        for row in store.execute(
            turns.invocation_columns(
                [invocation_id for invocation_id, _ in runs], AgentRun.id.in_(run_ids)
            )
        )
    }
    waiting = set(
        store.scalars(
            select(Approval.requested_by_run).where(
                Approval.requested_by_run.in_(run_ids), *not_given()
            )
        )
    )
    running = set(
        store.scalars(
            select(Invocation.id).where(
                Invocation.id.in_([i for i, _ in runs]),
                Invocation.status == InvocationStatus.running,
            )
        )
    )
    out = {}
    for invocation_id, run_id in runs:
        row = rows.get(invocation_id)
        if row is None:
            continue
        out[invocation_id] = AgentRunLiveOut.model_validate(
            {
                **row._mapping,
                "id": run_id,
                "waiting": run_id in waiting and invocation_id in running,
            }
        )
    return out


def with_runs(
    db: Session, store: Session, organisation_id: int, outs: list[InvocationOut]
) -> list[InvocationOut]:
    """The organisation's chat's invocations with their runs as the run page shows them, from
    its store, and what each queued mention of an agent waits for."""
    agent_runs = agent_runs_live(
        store, [(o.id, o.agent_run_id) for o in outs if o.agent_run_id is not None]
    )
    waits = invocations.waiting_for(
        store,
        [
            (o.id, o.chat_id, o.target_id)
            for o in outs
            if o.target_kind == TriggerTargetKind.agent
            and not o.delivered
            and o.status in invocations.NOT_STARTED
        ],
    )
    for out in outs:
        out.waiting_for = waits.get(out.id)
        if out.agent_run_id is not None:
            out.agent_run = agent_runs.get(out.id)
        if out.workflow_run_id is not None and store.get(WorkflowRun, out.workflow_run_id):
            out.workflow_run = workflows_api.workflow_run_out(
                db, organisation_id, store, out.workflow_run_id
            )
    return outs


def messages_out(
    db: Session, store: Session, organisation_id: int, chat_id: int, messages: list[ChatMessage]
) -> MessagesOut:
    """The messages, and the invocations they belong to and those of the chat still going."""
    names = author_names(db, store, messages)
    out = [message_out(m, names) for m in messages]
    found = store.scalars(
        select(Invocation)
        .where(
            or_(
                and_(
                    Invocation.chat_id == chat_id,
                    or_(
                        Invocation.id.in_([m.invocation_id for m in out if m.invocation_id]),
                        Invocation.status.in_(invocations.LIVE),
                    ),
                ),
                # Results delivered here: only with their message, posted when the run ends.
                and_(
                    Invocation.delivery_chat_id == chat_id,
                    Invocation.id.in_([m.invocation_id for m in out if m.invocation_id]),
                ),
            )
        )
        .order_by(Invocation.id)
    ).all()
    acknowledgements = dict(
        store.execute(
            select(ChatMessage.invocation_id, ChatMessage.body).where(
                ChatMessage.invocation_id.in_([i.id for i in found]),
                ChatMessage.kind == ChatMessageKind.run_card,
            )
        ).all()
    )
    return MessagesOut(
        messages=out,
        invocations=with_runs(
            db,
            store,
            organisation_id,
            [invocation_out(i, acknowledgements.get(i.id, "")) for i in found],
        ),
    )


def resolve_mentions(
    db: Session, store: Session, chat: Chat, mentions: list[MentionIn]
) -> list[MentionOut]:
    """The members a message mentions, each once in the order given, with their names. A 422 for
    a person who isn't a member, and one naming each agent or workflow the chat doesn't include
    (`detail.missing`: kind, id and name), for the screen to offer to add. An archived agent is
    still included."""
    found = mentionables(db, store, chat, archived=True)
    members = {(m.kind, m.id): m for m in [*found.users, *found.agents, *found.workflows]}
    resolved: dict[tuple[ChatMemberKind, int], MentionOut] = {}
    missing: dict[tuple[ChatMemberKind, int], MentionOut] = {}
    for mention in mentions:
        member = members.get((mention.kind, mention.id))
        if member is not None:
            resolved.setdefault(
                (member.kind, member.id),
                MentionOut(kind=member.kind, id=member.id, name=member.name),
            )
            continue
        if mention.kind == ChatMemberKind.user:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, NOT_MENTIONABLE)
        model = TARGET_MODELS[mention.kind]
        target = store.get(model, mention.id)
        if target is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {mention.kind.value}"
            )
        missing.setdefault(
            (mention.kind, mention.id),
            MentionOut(kind=mention.kind, id=target.id, name=target.name),
        )
    if missing:
        names = " or ".join(f"{m.name} ({m.kind.value})" for m in missing.values())
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "message": NOT_INCLUDED.format(names=names),
                "missing": [m.model_dump(mode="json") for m in missing.values()],
            },
        )
    return list(resolved.values())


def check_with_jev(body: str) -> None:
    """Jev's check of a person's message (`aios.jev`), before anything of it is written: a 422
    with Jev's reason if it's flagged, a 503 to try again if Jev can't check it within
    `jev_chat_timeout_seconds`. Run in the request's worker thread, outside any event loop."""
    try:
        judgement = asyncio.run(
            jev.check(body, SOURCE, timeout=get_settings().jev_chat_timeout_seconds)
        )
    except jev.JevUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNCHECKED) from None
    if judgement.flagged:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": FLAGGED, "reason": judgement.reason or ""},
        )


@router.get(
    "/chats/{chat_id}/messages", response_model=MessagesOut, dependencies=[allow(Action.read)]
)
def list_messages(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    after: Annotated[int | None, Query(ge=0)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
):
    """A page of the chat's messages, oldest first: the next ones after `after` (what's new), the
    ones just before `before` (history), or with neither the latest. A page is at most
    `MESSAGE_PAGE` messages; a shorter one is all there is."""
    if after is not None and before is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Give `after` or `before`, not both."
        )
    get_visible_chat(store, user.id, chat_id)
    query = select(ChatMessage).where(ChatMessage.chat_id == chat_id)
    if after is not None:
        query = query.where(ChatMessage.id > after).order_by(ChatMessage.id)
    else:
        if before is not None:
            query = query.where(ChatMessage.id < before)
        query = query.order_by(ChatMessage.id.desc())
    rows = list(store.scalars(query.limit(package.MESSAGE_PAGE)))
    if after is None:
        rows.reverse()
    return messages_out(db, store, org.id, chat_id, rows)


NOT_A_MEMBER_READING = "Only the chat's members keep track of what they've read in it."


class ReadIn(BaseModel):
    # The last of the chat's messages the user has seen: the newest, once they've read to the
    # bottom.
    message_id: DbId


@router.post(
    "/chats/{chat_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.post)],
)
def mark_read(
    chat_id: DbId,
    body: ReadIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
) -> None:
    """Note that the user, a member, has seen the chat's messages up to `message_id`, which
    clears their unread count of those, and their mention, reply and run failed notifications
    about them. Never back: a read of an earlier message (another tab's) leaves it, and never
    past the chat's newest message."""
    chat = get_visible_chat(store, user.id, chat_id)
    member = user_member(chat, user.id)
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_MEMBER_READING)
    seen = min(body.message_id, package.newest_message_id(store, chat.id) or 0)
    # In one statement, so two tabs reading at once leave the later of the two.
    store.execute(
        update(ChatMember)
        .where(ChatMember.id == member.id)
        .values(
            last_read_message_id=func.greatest(
                func.coalesce(ChatMember.last_read_message_id, 0), seen
            )
        )
    )
    store.commit()
    notifications.mark_read_in_chat(db, org.id, user.id, chat.id, seen)
    db.commit()


@router.post(
    "/chats/{chat_id}/messages",
    response_model=MessagesOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def post_message(
    chat_id: DbId,
    body: MessageIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
    background: BackgroundTasks,
):
    """Post a markdown message to the chat as the user, joining them to it if they haven't
    (in the same transaction). Returns it, and for each agent or workflow it mentions its
    acknowledgement with a card and its queued invocation, whose run starts after the response.

    The one path every person's message goes through. In order, before anything is written:
    more agents and workflows than a message may mention is a 422 (`aios.guards`); the chat
    rules (`aios.api.chats.rules`): an archived chat is a 409; the mention guards: a mention of
    an agent or workflow the chat doesn't include is a 422 naming it; a
    mention the poster may not make (its target's invocable-by) is posted refused, its card
    saying why; past the poster's mention rate, a 429 with when to try again; the organisation
    at its budget, or the poster at their allowance, posts every mention refused. Then Jev
    (`check_with_jev`): flagged is a 422 with the reason, unchecked a 503. None of those writes
    anything: no message, membership, invocation, search item or notification. The people it
    mentions are notified (`mention`), never the poster."""
    targets = list(
        dict.fromkeys(
            (TriggerTargetKind(m.kind.value), m.id)
            for m in body.mentions
            if m.kind in (ChatMemberKind.agent, ChatMemberKind.workflow)
        )
    )
    guards.check_count(targets)
    chat = get_visible_chat(store, user.id, chat_id)
    # An archived chat is read-only: a 409, before anything else (Jev's check included).
    rules.check(role, chat, user.id, ChatAction.post)
    mentions = resolve_mentions(db, store, chat, body.mentions)
    refused = guards.refusals(store, role, targets)
    guards.check_rate(org.id, user.id, len([t for t in targets if t not in refused]))
    if set(targets) - set(refused) and (reason := guards.check_budget(store, org.id, user.id)):
        refused |= {t: reason for t in targets if t not in refused}
    check_with_jev(body.body)
    # Held until the store has committed the message (and the poster's joining by it): one
    # leaving the organisation as they post waits, and is a 404 (`organisations.holds_member`).
    # Before the chat's lock, as leaving takes the organisation's before the chats'.
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not is_member(chat, user.id):
        # Locks the chat, as posting does anyway.
        join(store, chat, user.id)
    else:
        # An admin archiving it meanwhile (`lock_chat`).
        lock_chat(store, chat, writing=True)
    message = ChatMessage(
        chat_id=chat.id,
        author_kind=ChatAuthorKind.user,
        author_user_id=user.id,
        kind=ChatMessageKind.text,
        body=body.body,
        mentions=[m.model_dump(mode="json") for m in mentions],
        verdict=jev.Verdict.clean,
    )
    store.add(message)
    search_index.index_chat_message(store, message)
    outbox.message_posted(store, message)
    started = invocations.create(
        store,
        message,
        [
            (TriggerTargetKind(m.kind.value), m.id)
            for m in mentions
            if m.kind in (ChatMemberKind.agent, ChatMemberKind.workflow)
        ],
        refused,
    )
    # Posting is activity.
    chat.updated_at = func.now()
    store.commit()
    # The people it mentions are notified, never its poster; with the post, now the store has
    # it (`organisations.holds_member` keeps the poster in until this commits).
    notifications.notify(
        db,
        [m.id for m in mentions if m.kind == ChatMemberKind.user and m.id != user.id],
        org.id,
        notifications.MENTION,
        notifications.chat_ref(chat.id, message.id),
    )
    db.commit()
    store_id = store_id_of(store)
    invocation_ids = [i.id for i in started]
    if queued := [i.id for i in started if i.status == InvocationStatus.queued]:
        # On the run service, once the store has them; woken after the response.
        package.enqueue_starts(db, org.id, store_id, queued)
        background.add_task(run_service.wake, WorkLane.interactive)
    store.expire_all()
    posted = store.scalars(
        select(ChatMessage)
        .where(or_(ChatMessage.id == message.id, ChatMessage.invocation_id.in_(invocation_ids)))
        .order_by(ChatMessage.id)
    ).all()
    return messages_out(db, store, org.id, chat.id, list(posted))
