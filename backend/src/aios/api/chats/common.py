"""What the chats API's parts share: the chat's schemas, loading, lookups and locks."""

from datetime import datetime

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aios.api.provenance import Provenance, UserOut, provenance
from aios.db import DbId
from aios.models import (
    Agent,
    Chat,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatVisibility,
    Organisation,
    Workflow,
)

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
    # A user's full name ("First Last").
    name: str
    added_at: datetime
    # Whether it's an archived agent: still a member, shown as archived, but not offered to
    # mention (`GET /api/chats/{id}/mentionable`).
    archived: bool = False


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
            name=user.full_name,
            added_at=member.added_at,
        )
    target = member.agent if member.kind == ChatMemberKind.agent else member.workflow
    return ChatMemberOut(
        id=member.id,
        kind=member.kind,
        role=None,
        target_id=target.id,
        name=target.name,
        added_at=member.added_at,
        archived=target.archived_at is not None,
    )


def live_members(chat: Chat) -> list[ChatMember]:
    """The chat's members, less any workflow since archived (put away as a deleted one was; its
    member row stays): the owner first, the rest as they were added (ids go up with time). An
    archived agent stays a member, shown as archived."""
    live = [m for m in chat.members if m.workflow is None or m.workflow.archived_at is None]
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


def lock_chat(db: Session, chat: Chat) -> None:
    """Lock the chat until commit, for a change to it or to what's in it; a 404 if it has been
    deleted since `get_visible_chat` read it: by its owner, or by its owner leaving the
    organisation with nobody else in it (`organisations.hand_over_chats`, which locks it first).
    Under the lock that waits for the deletion and sees the chat gone, where adding a member or a
    message to it would break their foreign key (a 500), and a deletion that comes after waits
    and takes what the change added with the chat.
    `FOR NO KEY UPDATE`: the lock the change to `updated_at` each of them makes takes anyway, only
    sooner, so it doesn't hold up what that wouldn't (a message's foreign key takes a key share)."""
    locked = select(Chat.id).where(Chat.id == chat.id).with_for_update(key_share=True)
    if db.scalar(locked) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def lock_owner(db: Session, chat: Chat) -> int | None:
    """Lock the chat (`lock_chat`), and read who holds its owner role now (None: nobody), for a
    change to its members. The members `get_visible_chat` loaded can be out of date by then: the
    owner leaving the organisation deletes their member row and hands the chat on, or deletes it
    (`organisations.hand_over_chats`, which locks the chat first), and a join, add, removal or
    leaving decided on the owner it loaded would go in after that, leaving a member in a chat
    nobody owns. Under the lock it waits for the hand-over, or the hand-over waits for it and sees
    what it did."""
    lock_chat(db, chat)
    return db.scalar(
        select(ChatMember.user_id).where(
            ChatMember.chat_id == chat.id, ChatMember.role == ChatMemberRole.owner
        )
    )


OWNER_ONLY = "Only the chat's owner can do that."


def get_chat_if_owner(db: Session, org: Organisation, user_id: int, chat_id: int) -> Chat:
    """The chat, for one of the owner's actions: a 403 for anyone else it's visible to."""
    chat = get_visible_chat(db, org, user_id, chat_id)
    if not is_owner(chat, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWNER_ONLY)
    return chat


def still_owner(db: Session, chat: Chat, user_id: int) -> None:
    """A 403 unless the user still holds the chat's owner role, read under its lock
    (`lock_owner`), for an owner's change to its members: their leaving the organisation may
    have got in since `get_chat_if_owner`."""
    if lock_owner(db, chat) != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWNER_ONLY)
