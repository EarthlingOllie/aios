"""Chats: group conversations between users, agents and workflows.

A chat belongs to an organisation (`aios.organisations`), and doesn't exist for anyone outside it:
every endpoint is a 404 for them. Within it, a chat has an owner (the member who made it: a role in
the chat, held on their member row, not ownership of data), a name unique among the organisation's
chats, a description and a visibility, and says which member made it and last changed it
(`aios.api.provenance`). Its members are users, agents and workflows of the organisation (an agent
or workflow since deleted is no longer one; its row stays, and what it posted stays either way). A
private chat doesn't exist for a member who isn't in it either. A public one exists for every
member of the organisation: the list carries the public chats a member isn't in (`is_member` false)
for them to find, and they join it in one step and leave it as easily (the owner can't: a chat
always has one). Only the owner can change a chat, add or remove its members or delete it: a 403
for anyone else. An owner who leaves the organisation hands the chat to its member added first
(`organisations.leave`); with nobody left in it, whoever joins it next owns it. Leaving or being
removed deletes nothing, and whoever joins later reads the history from the start. A new chat's
first agent is the organisation's documents assistant (`aios.assistant`).

Each user member keeps the last message they've seen (`POST /api/chats/{id}/read`, as they read
to the bottom), and the list counts, per chat, the messages others posted since (`unread_count`).

Its members post markdown messages, read by id: `?after=N` for what's new (what the open chat
polls), `?before=N` for the page of history before N, and neither for the latest page. Anyone
the chat exists for can read them; only a member can post (a 403 for anyone else). A chat's last
activity is when it was last changed or a message was last posted.

A message can mention members: the composer picks them from `GET /api/chats/{id}/mentionable`, the
body carries a token for each (`@[name](kind:id)`) and the message stores them structurally,
`[{kind, id, name}]`, so an agent and a workflow with the same name are never confused. Mentioning
anyone who isn't a member is a 422.

Mentioning a workflow starts a run of it, with the message as its input (`aios.invocations`): the
response to the post carries the workflow's acknowledgement and run card with the message, and
the run starts just after (`InvocationFiring`). Before it does, the handoff (`aios.handoff`)
briefs it on the chat: the invocation is `briefing`, then its `brief` is kept and prepended to the
run's input, and its ack replaces the acknowledgement's placeholder (`acknowledgement`, which the
poll carries, so a card already shown follows it). A read's `invocations` are the state of each
invocation its messages belong to and of every one in the chat still going, each with its run
as the run's page shows it, so a chat's live cards cost its one poll. When the run ends, its
result is posted back as a message from the workflow.

Mentioning an agent runs one turn of a new run of it, with the brief and the message as the
user's, the same way: its acknowledgement and card come with the post, and the turn runs in this
process through the firing machinery schedules use (`TurnFiring`, `aios.firing`), under its cap:
it's claimed when there's a free slot (`launch_turns`, just after the post or on a scheduler
tick), beaten while it runs, and failed if its heartbeat stops (`fail_stopped_turns`), so a turn
lost with its process fails its card. A read's `invocations` carry an agent run's status, start
and counters (`agent_run`), and the agent's final reply is posted back when the turn ends.

Either run is started by the member who mentioned it (its `created_by`) and executes for the
organisation, over its store, like any other run: it doesn't depend on whoever made the agent or
workflow, and every member of the organisation reads it, in the chat or not.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from aios import assistant, firing, handoff, invocations
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.api.provenance import Provenance, UserOut, made_by, people, provenance
from aios.api.skills import lock_live, on_conflict, unique_name
from aios.auth import CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.db import DB, DbId, SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentRunStatus,
    Chat,
    ChatAuthorKind,
    ChatInvocation,
    ChatInvocationStatus,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatMessage,
    ChatMessageKind,
    ChatVisibility,
    Organisation,
    OrganisationMember,
    TriggerTargetKind,
    Workflow,
    WorkflowRun,
)
from aios.workflows import fail_stopped_runs

router = APIRouter(prefix="/api")

CHAT_NAME_INDEX = "uq_chats_organisation_id_name"
# What an agent or workflow member is.
TARGET_MODELS: dict[ChatMemberKind, type[Agent] | type[Workflow]] = {
    ChatMemberKind.agent: Agent,
    ChatMemberKind.workflow: Workflow,
}
# One row per (chat, user), (chat, agent) and (chat, workflow).
MEMBER_CONSTRAINTS = {
    ChatMemberKind.user: "chat_members_chat_id_user_id_key",
    ChatMemberKind.agent: "chat_members_chat_id_agent_id_key",
    ChatMemberKind.workflow: "chat_members_chat_id_workflow_id_key",
}
ALREADY_A_MEMBER = "Already a member of this chat."


class ChatIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    visibility: ChatVisibility = ChatVisibility.private


class MemberIn(BaseModel):
    kind: ChatMemberKind
    # The user's, agent's or workflow's id: a member of the organisation, or one of its agents
    # or workflows.
    id: DbId


class ChatMemberOut(BaseModel):
    # The member row's id, which `DELETE /api/chats/{id}/members/{member_id}` takes.
    id: int
    kind: ChatMemberKind
    # Users only.
    role: ChatMemberRole | None
    # The user's, agent's or workflow's id.
    target_id: int
    name: str
    # Users only.
    login: str | None
    avatar_url: str | None
    added_at: datetime


class ChatMembersOut(BaseModel):
    """A chat's members, grouped by kind: the owner first, then in the order they were added."""

    users: list[ChatMemberOut]
    agents: list[ChatMemberOut]
    workflows: list[ChatMemberOut]


class ChatSummary(Provenance):
    id: int
    name: str
    description: str
    visibility: ChatVisibility
    # Whether the signed-in user is its owner (their member row's role), so the UI knows to offer
    # the owner's actions.
    is_owner: bool
    # Whether the signed-in user is in it, and so can post (a public chat can be read by anyone).
    is_member: bool
    member_count: int
    created_at: datetime
    # When it was last active: when it was last changed, or a message was last posted.
    updated_at: datetime


class ChatOut(ChatSummary):
    members: ChatMembersOut


def member_out(member: ChatMember) -> ChatMemberOut:
    if member.kind == ChatMemberKind.user:
        user = member.user
        return ChatMemberOut(
            id=member.id,
            kind=member.kind,
            role=member.role,
            target_id=user.id,
            name=user.name or user.login,
            login=user.login,
            avatar_url=user.avatar_url,
            added_at=member.added_at,
        )
    target = member.agent if member.kind == ChatMemberKind.agent else member.workflow
    return ChatMemberOut(
        id=member.id,
        kind=member.kind,
        role=None,
        target_id=target.id,
        name=target.name,
        login=None,
        avatar_url=None,
        added_at=member.added_at,
    )


def live_members(chat: Chat) -> list[ChatMember]:
    """The chat's members, less any agent or workflow since deleted (they're soft-deleted, and
    their member rows stay): the owner first, the rest as they were added (ids go up with time)."""
    live = [
        m
        for m in chat.members
        if (m.agent is None or m.agent.deleted_at is None)
        and (m.workflow is None or m.workflow.deleted_at is None)
    ]
    return sorted(live, key=lambda m: (m.role != ChatMemberRole.owner, m.id))


def members_out(chat: Chat) -> ChatMembersOut:
    by_kind: dict[ChatMemberKind, list[ChatMemberOut]] = {kind: [] for kind in ChatMemberKind}
    for member in live_members(chat):
        by_kind[member.kind].append(member_out(member))
    return ChatMembersOut(
        users=by_kind[ChatMemberKind.user],
        agents=by_kind[ChatMemberKind.agent],
        workflows=by_kind[ChatMemberKind.workflow],
    )


def chat_summary(
    db: Session,
    chat: Chat,
    member_count: int,
    role: ChatMemberRole | None,
    known: dict[int, UserOut] | None = None,
) -> ChatSummary:
    """The chat as the list shows it, to a user with this `role` in it (None: not a member);
    who made and changed it from `known` when given (see `people`), else read now."""
    return ChatSummary(
        **provenance(db, chat, known),
        id=chat.id,
        name=chat.name,
        description=chat.description,
        visibility=chat.visibility,
        is_owner=role == ChatMemberRole.owner,
        is_member=role is not None,
        member_count=member_count,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def chat_out(db: Session, chat: Chat, user_id: int) -> ChatOut:
    member = user_member(chat, user_id)
    return ChatOut(
        **chat_summary(
            db, chat, len(live_members(chat)), member.role if member else None
        ).model_dump(),
        members=members_out(chat),
    )


LOAD = (
    selectinload(Chat.members).selectinload(ChatMember.user),
    selectinload(Chat.members).selectinload(ChatMember.agent),
    selectinload(Chat.members).selectinload(ChatMember.workflow),
)


def load(db: Session, chat_id: int) -> Chat:
    """The chat fresh from the database, with its members and what they point at."""
    db.expire_all()
    return db.scalars(select(Chat).where(Chat.id == chat_id).options(*LOAD)).one()


def member_key(member: ChatMember) -> tuple[ChatMemberKind, int | None]:
    """What a member is: its kind and the user's, agent's or workflow's id."""
    return member.kind, member.user_id or member.agent_id or member.workflow_id


def user_member(chat: Chat, user_id: int) -> ChatMember | None:
    """The user's membership of the chat, if they're in it."""
    return next(
        (m for m in chat.members if m.kind == ChatMemberKind.user and m.user_id == user_id), None
    )


def is_member(chat: Chat, user_id: int) -> bool:
    return user_member(chat, user_id) is not None


def get_visible_chat(db: Session, org: Organisation, user_id: int, chat_id: int) -> Chat:
    """The chat, if it exists for the user: any public chat of their organisation, or a private
    one of it they're in."""
    chat = db.scalars(
        select(Chat).where(Chat.id == chat_id, Chat.organisation_id == org.id).options(*LOAD)
    ).one_or_none()
    if chat is None or (chat.visibility == ChatVisibility.private and not is_member(chat, user_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return chat


def is_owner(chat: Chat, user_id: int) -> bool:
    """Whether the user holds the chat's owner role."""
    member = user_member(chat, user_id)
    return member is not None and member.role == ChatMemberRole.owner


def get_chat_if_owner(db: Session, org: Organisation, user_id: int, chat_id: int) -> Chat:
    """The chat, for one of the owner's actions: a 403 for anyone else it's visible to."""
    chat = get_visible_chat(db, org, user_id, chat_id)
    if not is_owner(chat, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the chat's owner can do that.")
    return chat


class ChatListed(ChatSummary):
    # How many of its messages others posted since the last one the user saw (`POST
    # /api/chats/{id}/read`); null for a public chat they're not in.
    unread_count: int | None


@router.get("/chats", response_model=list[ChatListed])
def list_chats(user: CurrentUser, org: CurrentOrganisation, db: DB):
    """The organisation's chats the user is a member of, and its public ones they aren't
    (`is_member` false), the most recently active first."""
    mine = aliased(ChatMember)
    member_count = (
        select(func.count(ChatMember.id))
        # Not counting agents or workflows since deleted (see `live_members`).
        .outerjoin(Agent, Agent.id == ChatMember.agent_id)
        .outerjoin(Workflow, Workflow.id == ChatMember.workflow_id)
        .where(
            ChatMember.chat_id == Chat.id,
            Agent.deleted_at.is_(None),
            Workflow.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    unread = case(
        (mine.id.is_(None), None),
        else_=select(func.count(ChatMessage.id))
        .where(
            ChatMessage.chat_id == Chat.id,
            ChatMessage.id > func.coalesce(mine.last_read_message_id, 0),
            # What they posted themselves they've seen.
            ChatMessage.author_user_id.is_distinct_from(user.id),
        )
        .scalar_subquery(),
    )
    rows = db.execute(
        select(Chat, member_count, mine.role, unread)
        .outerjoin(mine, and_(mine.chat_id == Chat.id, mine.user_id == user.id))
        .where(
            Chat.organisation_id == org.id,
            or_(mine.id.is_not(None), Chat.visibility == ChatVisibility.public),
        )
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
    ).all()
    known = people(db, [i for row in rows for i in (row[0].created_by, row[0].updated_by)])
    return [
        ChatListed(
            **chat_summary(db, chat, count, role, known=known).model_dump(),
            unread_count=unread_count,
        )
        for chat, count, role, unread_count in rows
    ]


@router.post("/chats", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
def create_chat(body: ChatIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Make a chat of the organisation's, with the user as its owner and first member, and the
    organisation's documents assistant (`aios.assistant`) as its first agent."""
    chat = Chat(
        **made_by(org.id, user.id),
        name=body.name,
        description=body.description,
        visibility=body.visibility,
    )
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.user,
            user_id=user.id,
            role=ChatMemberRole.owner,
            added_by=user.id,
        )
    )
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.agent,
            agent=assistant.documents_assistant(db, org, user.id),
            added_by=user.id,
        )
    )
    db.add(chat)
    with unique_name(db, "a chat", CHAT_NAME_INDEX):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


@router.get("/chats/{chat_id}", response_model=ChatOut)
def get_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    return chat_out(db, get_visible_chat(db, org, user.id, chat_id), user.id)


class MentionableOut(BaseModel):
    """A member as the composer offers it to mention, and as a message's `mentions` name it."""

    kind: ChatMemberKind
    # The user's, agent's or workflow's id.
    id: int
    name: str
    # Users only.
    login: str | None
    avatar_url: str | None


class MentionablesOut(BaseModel):
    """Who can be mentioned in a chat: its members, grouped by kind, as `ChatMembersOut` orders
    them. An agent and a workflow can share a name; their kinds tell them apart."""

    users: list[MentionableOut]
    agents: list[MentionableOut]
    workflows: list[MentionableOut]


def mentionable_out(member: ChatMemberOut) -> MentionableOut:
    return MentionableOut(
        kind=member.kind,
        id=member.target_id,
        name=member.name,
        login=member.login,
        avatar_url=member.avatar_url,
    )


def mentionables(chat: Chat) -> MentionablesOut:
    members = members_out(chat)
    return MentionablesOut(
        users=[mentionable_out(m) for m in members.users],
        agents=[mentionable_out(m) for m in members.agents],
        workflows=[mentionable_out(m) for m in members.workflows],
    )


@router.get("/chats/{chat_id}/mentionable", response_model=MentionablesOut)
def list_mentionable(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """The chat's members, for the composer's `@` autocomplete."""
    return mentionables(get_visible_chat(db, org, user.id, chat_id))


@router.put("/chats/{chat_id}", response_model=ChatOut)
def update_chat(chat_id: DbId, body: ChatIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Rename, describe or change the visibility of the chat (its owner only)."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    chat.name = body.name
    chat.description = body.description
    chat.visibility = body.visibility
    chat.updated_by = user.id
    with unique_name(db, "a chat", CHAT_NAME_INDEX):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


@router.post(
    "/chats/{chat_id}/members", response_model=ChatOut, status_code=status.HTTP_201_CREATED
)
def add_member(chat_id: DbId, body: MemberIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Add a member of the organisation, or one of its agents or workflows, to the chat (its
    owner only). A user outside the organisation doesn't exist here: a 404, as one who doesn't
    exist at all is."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    if body.kind == ChatMemberKind.user:
        if db.get(OrganisationMember, (org.id, body.id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")
        member = ChatMember(
            kind=body.kind, user_id=body.id, role=ChatMemberRole.member, added_by=user.id
        )
    else:
        model = TARGET_MODELS[body.kind]
        target = db.get(model, body.id)
        # Locked until commit, so a concurrent delete either waits for the add or wins it.
        if (
            target is None
            or target.organisation_id != org.id
            or not lock_live(db, model, [target.id], read=True)
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {body.kind}")
        member = ChatMember(kind=body.kind, added_by=user.id, **{f"{body.kind}_id": target.id})
    if member_key(member) in {member_key(m) for m in chat.members}:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    chat.members.append(member)
    # Adding a member is activity too.
    chat.updated_at = func.now()
    # The check above can lose a race with another add of the same member; this catches that.
    with on_conflict(db, MEMBER_CONSTRAINTS[body.kind], ALREADY_A_MEMBER):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


def has_owner(db: Session, chat: Chat) -> bool:
    """Whether someone holds the chat's owner role. Locks the chat when nobody does, so two
    joining an ownerless chat at once don't both become its owner: the second sees the first."""
    if any(m.role == ChatMemberRole.owner for m in chat.members):
        return True
    db.scalar(select(Chat.id).where(Chat.id == chat.id).with_for_update())
    return (
        db.scalar(
            select(ChatMember.id).where(
                ChatMember.chat_id == chat.id, ChatMember.role == ChatMemberRole.owner
            )
        )
        is not None
    )


def newest_message_id(db: Session, chat_id: int) -> int | None:
    """The id of the chat's newest message; None while it has none."""
    return db.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.chat_id == chat_id))


@router.post("/chats/{chat_id}/join", response_model=ChatOut)
def join_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Join a public chat as a member (a private one exists only for its members), or as its
    owner if it has none (its owner left the organisation with nobody in it to hand it to:
    `organisations.leave`). What was posted before they joined counts as seen: they could read
    it, and it's all there to scroll."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    if is_member(chat, user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.user,
            user_id=user.id,
            role=ChatMemberRole.member if has_owner(db, chat) else ChatMemberRole.owner,
            added_by=user.id,
            last_read_message_id=newest_message_id(db, chat.id),
        )
    )
    # Joining is activity, as adding a member is.
    chat.updated_at = func.now()
    # Two joins at once: the second loses here.
    with on_conflict(db, MEMBER_CONSTRAINTS[ChatMemberKind.user], ALREADY_A_MEMBER):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


@router.post("/chats/{chat_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB) -> None:
    """Leave a chat the user is a member of but doesn't own (a chat always has its owner).
    Nothing they posted goes with them; a private chat stops existing for them."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    member = user_member(chat, user.id)
    if member is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Not a member of this chat.")
    if member.role == ChatMemberRole.owner:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The owner can't leave the chat."
        )
    chat.members.remove(member)
    # As removing a member is.
    chat.updated_at = func.now()
    db.commit()


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB) -> None:
    """Delete the chat and its members (its owner only)."""
    db.delete(get_chat_if_owner(db, org, user.id, chat_id))
    db.commit()


@router.delete("/chats/{chat_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    chat_id: DbId, member_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB
) -> None:
    """Remove a member from the chat (its owner only). The owner stays: a chat always has one."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    member = next((m for m in chat.members if m.id == member_id), None)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if member.role == ChatMemberRole.owner:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The owner can't be removed from the chat."
        )
    chat.members.remove(member)
    chat.updated_at = func.now()
    db.commit()


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
    name: str
    # Users only.
    login: str | None
    avatar_url: str | None


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
    status: ChatInvocationStatus
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


def author_out(message: ChatMessage) -> MessageAuthorOut | None:
    if message.author_user is not None:
        user = message.author_user
        return MessageAuthorOut(
            kind=ChatAuthorKind.user,
            id=user.id,
            name=user.name or user.login,
            login=user.login,
            avatar_url=user.avatar_url,
        )
    target = message.author_agent or message.author_workflow
    if target is None:
        return None
    return MessageAuthorOut(
        kind=message.author_kind, id=target.id, name=target.name, login=None, avatar_url=None
    )


def message_out(message: ChatMessage) -> MessageOut:
    return MessageOut(
        id=message.id,
        chat_id=message.chat_id,
        author=author_out(message),
        kind=message.kind,
        body=message.body,
        mentions=message.mentions,
        invocation_id=message.invocation_id,
        created_at=message.created_at,
    )


def invocation_out(invocation: ChatInvocation, acknowledgement: str) -> InvocationOut:
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


def agent_runs_live(db: Session, run_ids: list[int]) -> dict[int, AgentRunLiveOut]:
    """How each of these agent runs is going, by id; without a query when there are none."""
    if not run_ids:
        return {}
    rows = db.execute(
        select(
            AgentRun.id,
            AgentRun.status,
            AgentRun.started_at,
            AgentRun.finished_at,
            AgentRun.tool_call_count,
            AgentRun.input_tokens,
            AgentRun.output_tokens,
        ).where(AgentRun.id.in_(run_ids))
    )
    return {row.id: AgentRunLiveOut.model_validate(row, from_attributes=True) for row in rows}


def with_runs(db: Session, outs: list[InvocationOut]) -> list[InvocationOut]:
    """The invocations with their runs as the run page shows them. Read once the rest has been,
    since reading a run expires the session's rows."""
    agent_runs = agent_runs_live(db, [o.agent_run_id for o in outs if o.agent_run_id is not None])
    for out in outs:
        if out.agent_run_id is not None:
            out.agent_run = agent_runs.get(out.agent_run_id)
        if out.workflow_run_id is not None:
            out.workflow_run = workflows_api.workflow_run_out(db, out.workflow_run_id)
    return outs


def messages_out(db: Session, chat_id: int, messages: list[ChatMessage]) -> MessagesOut:
    """The messages, and the invocations they belong to and those of the chat still going."""
    out = [message_out(m) for m in messages]
    found = db.scalars(
        select(ChatInvocation)
        .where(
            ChatInvocation.chat_id == chat_id,
            or_(
                ChatInvocation.id.in_([m.invocation_id for m in out if m.invocation_id]),
                ChatInvocation.status.in_(invocations.UNFINISHED),
            ),
        )
        .order_by(ChatInvocation.id)
    ).all()
    acknowledgements = dict(
        db.execute(
            select(ChatMessage.invocation_id, ChatMessage.body).where(
                ChatMessage.invocation_id.in_([i.id for i in found]),
                ChatMessage.kind == ChatMessageKind.run_card,
            )
        ).all()
    )
    return MessagesOut(
        messages=out,
        invocations=with_runs(db, [invocation_out(i, acknowledgements[i.id]) for i in found]),
    )


def catch_up(db: Session, chat_id: int) -> None:
    """Bring the chat's unfinished invocations up to date before they're read: fail their runs
    whose worker died (which posts their results back), their agent turns whose process died,
    and those that never started. Commits."""
    fail_stopped_runs(
        db,
        WorkflowRun.id.in_(
            select(ChatInvocation.workflow_run_id).where(
                ChatInvocation.chat_id == chat_id,
                ChatInvocation.status.in_(invocations.UNFINISHED),
            )
        ),
    )
    fail_stopped_turns(db, chat_id)
    invocations.fail_stalled(db, chat_id)


MESSAGE_LOAD = (
    selectinload(ChatMessage.author_user),
    selectinload(ChatMessage.author_agent),
    selectinload(ChatMessage.author_workflow),
)


def resolve_mentions(chat: Chat, mentions: list[MentionIn]) -> list[MentionOut]:
    """The members a message mentions, each once in the order given, with their names: a 422 if
    any isn't a (live) member of the chat."""
    found = mentionables(chat)
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


@router.get("/chats/{chat_id}/messages", response_model=MessagesOut)
def list_messages(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
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
    get_visible_chat(db, org, user.id, chat_id)
    catch_up(db, chat_id)
    query = select(ChatMessage).where(ChatMessage.chat_id == chat_id).options(*MESSAGE_LOAD)
    if after is not None:
        query = query.where(ChatMessage.id > after).order_by(ChatMessage.id)
    else:
        if before is not None:
            query = query.where(ChatMessage.id < before)
        query = query.order_by(ChatMessage.id.desc())
    rows = list(db.scalars(query.limit(MESSAGE_PAGE)))
    if after is None:
        rows.reverse()
    return messages_out(db, chat_id, rows)


NOT_A_MEMBER_READING = "Only the chat's members keep track of what they've read in it."


class ReadIn(BaseModel):
    # The last of the chat's messages the user has seen: the newest, once they've read to the
    # bottom.
    message_id: DbId


@router.post("/chats/{chat_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(
    chat_id: DbId, body: ReadIn, user: CurrentUser, org: CurrentOrganisation, db: DB
) -> None:
    """Note that the user, a member, has seen the chat's messages up to `message_id`, which
    clears their unread count of those. Never back: a read of an earlier message (another tab's)
    leaves it, and never past the chat's newest message."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    member = user_member(chat, user.id)
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_MEMBER_READING)
    seen = min(body.message_id, newest_message_id(db, chat.id) or 0)
    # In one statement, so two tabs reading at once leave the later of the two.
    db.execute(
        update(ChatMember)
        .where(ChatMember.id == member.id)
        .values(
            last_read_message_id=func.greatest(
                func.coalesce(ChatMember.last_read_message_id, 0), seen
            )
        )
    )
    db.commit()


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
        try:
            run = workflows_api.new_workflow_run(
                db,
                invocation.workflow,
                invocations.run_input(invocation.brief, message.body),
                created_by=invocation.requested_by,
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                raise firing.CantStart("The workflow has been deleted.") from exc
            raise
        invocation.workflow_run_id = run.id
        # Commits the run and the link to it; if it can't be queued the run is deleted, and the
        # link with it (SET NULL).
        workflows_api.queue_workflow_run(db, run)
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
        agent = invocation.agent
        body = db.get(ChatMessage, invocation.message_id).body
        prompt = invocations.run_input(invocation.brief, body)
        try:
            # The agent's lock first, then the invocation's, as deleting the agent takes them.
            run = agents_api.new_agent_run(db, agent, created_by=invocation.requested_by)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                raise firing.CantStart("The agent has been deleted.") from exc
            raise
        invocation = invocations.lock(db, self.invocation_id)
        if invocation is None or invocation.status != ChatInvocationStatus.briefing:
            db.rollback()
            return None
        invocation.agent_run_id = run.id
        invocations.mark_running(db, self.invocation_id)
        db.commit()
        # Titled after what was asked.
        return firing.Turn(agent.id, run.id, prompt, title=invocations.message_text(body))

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


@router.post(
    "/chats/{chat_id}/messages", response_model=MessagesOut, status_code=status.HTTP_201_CREATED
)
def post_message(
    chat_id: DbId,
    body: MessageIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    background: BackgroundTasks,
):
    """Post a markdown message to the chat as the user, who must be a member of it. Returns it,
    and for each workflow it mentions the workflow's acknowledgement with a card and its queued
    invocation, whose run starts after the response."""
    chat = get_visible_chat(db, org, user.id, chat_id)
    if not is_member(chat, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_A_MEMBER)
    mentions = resolve_mentions(chat, body.mentions)
    message = ChatMessage(
        chat_id=chat.id,
        author_kind=ChatAuthorKind.user,
        author_user_id=user.id,
        kind=ChatMessageKind.text,
        body=body.body,
        mentions=[m.model_dump(mode="json") for m in mentions],
    )
    db.add(message)
    started = invocations.create(
        db,
        message,
        [
            (TriggerTargetKind(m.kind.value), m.id)
            for m in mentions
            if m.kind in (ChatMemberKind.agent, ChatMemberKind.workflow)
        ],
    )
    # Posting is activity.
    chat.updated_at = func.now()
    db.commit()
    invocation_ids = [i.id for i in started]
    if invocation_ids:
        if workflow_ids := [i.id for i in started if i.target_kind == TriggerTargetKind.workflow]:
            background.add_task(start_invocations, workflow_ids)
        if any(i.target_kind == TriggerTargetKind.agent for i in started):
            background.add_task(run_turns)
    db.expire_all()
    posted = db.scalars(
        select(ChatMessage)
        .where(or_(ChatMessage.id == message.id, ChatMessage.invocation_id.in_(invocation_ids)))
        .options(*MESSAGE_LOAD)
        .order_by(ChatMessage.id)
    ).all()
    return messages_out(db, chat.id, list(posted))
