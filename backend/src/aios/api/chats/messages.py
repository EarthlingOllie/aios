"""A chat's messages: reading them, keeping track of what's been read, and posting."""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aios import invocations, turns

# Read on the package at call time, where tests replace them.
from aios.api import chats as package
from aios.api import workflows as workflows_api
from aios.api.chats.common import (
    get_visible_chat,
    is_member,
    lock_chat,
    user_member,
    users_by_id,
)
from aios.api.chats.invocations import fail_stopped_turns, run_turns
from aios.api.chats.members import mentionables
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import store_id_of
from aios.datastores.models import (
    Agent,
    AgentRun,
    Chat,
    ChatMember,
    ChatMessage,
    Invocation,
    SchemaChangeRequest,
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
    Role,
    TriggerTargetKind,
)
from aios.permissions import MENTIONS_NOT_YET, Action, CurrentRole, allow
from aios.workflows import fail_stopped_runs

router = APIRouter(prefix="/api")


# How many messages a read returns at most: the latest page, a page of history, or what's new.
MESSAGE_PAGE = 50
NOT_A_MEMBER = "Only the chat's members can post in it."
NOT_MENTIONABLE = "Only the chat's members can be mentioned in it."


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
    """A run a mention started, as its card follows it."""

    id: int
    chat_id: int
    # The message that mentioned it, and who posted it (null once they're deleted).
    message_id: int
    requested_by: int | None
    target_kind: TriggerTargetKind
    # The agent's or workflow's id.
    target_id: int
    status: InvocationStatus
    # Why it failed: the run's error, or why it couldn't start.
    error: str | None
    # The acknowledgement's text as it is now: its placeholder until the handoff writes what the
    # member understood it's being asked to do (the run card's message has the same).
    acknowledgement: str
    # What the handoff told the run of the chat, before the message; null until it has.
    brief: str | None
    workflow_run_id: int | None
    agent_run_id: int | None
    # Whether it started a run that has since been deleted.
    run_deleted: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # The workflow run as its page shows it (graph, steps and all); null without one.
    workflow_run: workflows_api.WorkflowRunOut | None
    # The agent run's status, times and counters so far; null without one.
    agent_run: AgentRunLiveOut | None


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
        chat_id=invocation.chat_id,
        message_id=invocation.message_id,
        requested_by=invocation.requested_by,
        target_kind=invocation.target_kind,
        target_id=invocation.workflow_id or invocation.agent_id,
        status=invocation.status,
        error=invocation.error,
        acknowledgement=acknowledgement,
        brief=invocation.brief,
        workflow_run_id=invocation.workflow_run_id,
        agent_run_id=invocation.agent_run_id,
        run_deleted=invocation.started_at is not None and run_id is None,
        created_at=invocation.created_at,
        started_at=invocation.started_at,
        finished_at=invocation.finished_at,
        workflow_run=None,
        agent_run=None,
    )


def agent_runs_live(store: Session, run_ids: list[int]) -> dict[int, AgentRunLiveOut]:
    """How each of these agent runs (in the store `store` is a session on) is going, by id;
    without a query when there are none."""
    if not run_ids:
        return {}
    rows = store.execute(
        turns.run_columns(select(AgentRun.id)).where(AgentRun.id.in_(run_ids))
    ).all()
    waiting = set(
        store.scalars(
            select(SchemaChangeRequest.requested_by_run).where(
                SchemaChangeRequest.requested_by_run.in_(run_ids), *not_given()
            )
        )
    )
    return {
        row.id: AgentRunLiveOut.model_validate({**row._mapping, "waiting": row.id in waiting})
        for row in rows
    }


def with_runs(
    db: Session, store: Session, organisation_id: int, outs: list[InvocationOut]
) -> list[InvocationOut]:
    """The organisation's chat's invocations with their runs as the run page shows them, from
    its store."""
    agent_runs = agent_runs_live(store, [o.agent_run_id for o in outs if o.agent_run_id])
    for out in outs:
        if out.agent_run_id is not None:
            out.agent_run = agent_runs.get(out.agent_run_id)
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
            Invocation.chat_id == chat_id,
            or_(
                Invocation.id.in_([m.invocation_id for m in out if m.invocation_id]),
                Invocation.status.in_(invocations.UNFINISHED),
            ),
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
            [invocation_out(i, acknowledgements[i.id]) for i in found],
        ),
    )


def catch_up(store: Session, chat_id: int) -> None:
    """Bring the chat's unfinished invocations up to date before they're read: fail their runs
    whose worker died (which posts their results back), their agent turns whose process died,
    and those that never started. Commits."""
    run_ids = store.scalars(
        select(Invocation.workflow_run_id).where(
            Invocation.chat_id == chat_id,
            Invocation.status.in_(invocations.UNFINISHED),
            Invocation.workflow_run_id.is_not(None),
        )
    ).all()
    if run_ids:
        fail_stopped_runs(store, WorkflowRun.id.in_(run_ids))
    fail_stopped_turns(store, chat_id)
    invocations.fail_stalled(store, chat_id)


def resolve_mentions(
    db: Session, store: Session, chat: Chat, mentions: list[MentionIn]
) -> list[MentionOut]:
    """The members a message mentions, each once in the order given, with their names: a 422 if
    any isn't a member of the chat. An archived agent still is."""
    found = mentionables(db, store, chat, archived=True)
    members = {(m.kind, m.id): m for m in [*found.users, *found.agents, *found.workflows]}
    resolved: dict[tuple[ChatMemberKind, int], MentionOut] = {}
    for mention in mentions:
        member = members.get((mention.kind, mention.id))
        if member is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, NOT_MENTIONABLE)
        resolved.setdefault(
            (member.kind, member.id), MentionOut(kind=member.kind, id=member.id, name=member.name)
        )
    return list(resolved.values())


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
    catch_up(store, chat_id)
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
def mark_read(chat_id: DbId, body: ReadIn, user: CurrentUser, store: CurrentStore) -> None:
    """Note that the user, a member, has seen the chat's messages up to `message_id`, which
    clears their unread count of those. Never back: a read of an earlier message (another tab's)
    leaves it, and never past the chat's newest message."""
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
    """Post a markdown message to the chat as the user, who must be a member of it. Returns it,
    and for each workflow it mentions the workflow's acknowledgement with a card and its queued
    invocation, whose run starts after the response. A participant's mention of an agent or a
    workflow is refused (403, saying why) until mentions are guarded."""
    if role == Role.participant and any(
        m.kind in (ChatMemberKind.agent, ChatMemberKind.workflow) for m in body.mentions
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, MENTIONS_NOT_YET)
    chat = get_visible_chat(store, user.id, chat_id)
    if not is_member(chat, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_MEMBER)
    # Its owner leaving the organisation may be deleting it (`lock_chat`).
    lock_chat(store, chat)
    mentions = resolve_mentions(db, store, chat, body.mentions)
    message = ChatMessage(
        chat_id=chat.id,
        author_kind=ChatAuthorKind.user,
        author_user_id=user.id,
        kind=ChatMessageKind.text,
        body=body.body,
        mentions=[m.model_dump(mode="json") for m in mentions],
    )
    store.add(message)
    started = invocations.create(
        store,
        message,
        [
            (TriggerTargetKind(m.kind.value), m.id)
            for m in mentions
            if m.kind in (ChatMemberKind.agent, ChatMemberKind.workflow)
        ],
    )
    # Posting is activity.
    chat.updated_at = func.now()
    store.commit()
    store_id = store_id_of(store)
    invocation_ids = [i.id for i in started]
    if invocation_ids:
        if workflow_ids := [i.id for i in started if i.target_kind == TriggerTargetKind.workflow]:
            background.add_task(package.start_invocations, store_id, workflow_ids)
        if any(i.target_kind == TriggerTargetKind.agent for i in started):
            background.add_task(run_turns, store_id)
    store.expire_all()
    posted = store.scalars(
        select(ChatMessage)
        .where(or_(ChatMessage.id == message.id, ChatMessage.invocation_id.in_(invocation_ids)))
        .order_by(ChatMessage.id)
    ).all()
    return messages_out(db, store, org.id, chat.id, list(posted))
