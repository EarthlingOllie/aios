"""What the chats API's parts share: the chat's schemas, loading, lookups and locks."""

from collections.abc import Iterable
from datetime import datetime

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aios.api.provenance import Provenance, UserOut, people, provenance
from aios.datastores.models import Agent, Chat, ChatMember, Workflow
from aios.db import DbId
from aios.models import (
    ChatMemberKind,
    ChatMemberRole,
    ChatVisibility,
    User,
)

CHAT_NAME_INDEX = "uq_chats_name"
# What an agent or workflow member is.
TARGET_MODELS: dict[ChatMemberKind, type[Agent] | type[Workflow]] = {
    ChatMemberKind.agent: Agent,
    ChatMemberKind.workflow: Workflow,
}
# One row per (chat, user), (chat, agent) and (chat, workflow).
MEMBER_CONSTRAINTS = {
    ChatMemberKind.user: "uq_chat_members_chat_id_user_id",
    ChatMemberKind.agent: "uq_chat_members_chat_id_agent_id",
    ChatMemberKind.workflow: "uq_chat_members_chat_id_workflow_id",
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
    # Whether it's an archived agent or workflow: still a member, shown as archived, but not
    # offered to mention (`GET /api/chats/{id}/mentionable`).
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


# An agent or workflow member's name, and whether it's archived, by (kind, id).
Targets = dict[tuple[ChatMemberKind, int], tuple[str, bool]]
# The people who are members, by user id: chats are in the store, people on the platform.
Users = dict[int, User]


def chat_targets(store: Session, chat: Chat) -> Targets:
    """The chat's agent and workflow members as they are in its store: their names, and whether
    each is archived."""
    found: Targets = {}
    wanted = {
        kind: [m.agent_id or m.workflow_id for m in chat.members if m.kind == kind]
        for kind in TARGET_MODELS
    }
    for kind, ids in wanted.items():
        if not ids:
            continue
        model = TARGET_MODELS[kind]
        for i, name, archived_at in store.execute(
            select(model.id, model.name, model.archived_at).where(model.id.in_(ids))
        ):
            found[kind, i] = (name, archived_at is not None)
    return found


def users_by_id(db: Session, ids: Iterable[int | None]) -> Users:
    """The platform's users with these ids (nulls skipped), by id: the chat's people."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(wanted)))}


def chat_users(db: Session, chat: Chat) -> Users:
    """The chat's people members, from the platform."""
    return users_by_id(db, [m.user_id for m in chat.members])


def member_out(member: ChatMember, targets: Targets, users: Users) -> ChatMemberOut | None:
    """The member, an agent or workflow one as `targets` (`chat_targets`) has it, a person as
    `users` (`chat_users`) does: None for a person who no longer exists."""
    if member.kind == ChatMemberKind.user:
        user = users.get(member.user_id)
        if user is None:
            return None
        return ChatMemberOut(
            id=member.id,
            kind=member.kind,
            role=member.role,
            target_id=user.id,
            name=user.full_name,
            added_at=member.added_at,
        )
    target_id = member.agent_id or member.workflow_id
    name, archived = targets.get((member.kind, target_id), ("", False))
    return ChatMemberOut(
        id=member.id,
        kind=member.kind,
        role=None,
        target_id=target_id,
        name=name,
        added_at=member.added_at,
        archived=archived,
    )


def ordered_members(chat: Chat) -> list[ChatMember]:
    """The chat's members: the owner first, the rest as they were added (ids go up with time).
    An archived agent or workflow stays a member, shown as archived."""
    return sorted(chat.members, key=lambda m: (m.role != ChatMemberRole.owner, m.id))


def members_out(db: Session, store: Session, chat: Chat) -> ChatMembersOut:
    targets = chat_targets(store, chat)
    users = chat_users(db, chat)
    by_kind: dict[ChatMemberKind, list[ChatMemberOut]] = {kind: [] for kind in ChatMemberKind}
    for member in ordered_members(chat):
        if (out := member_out(member, targets, users)) is not None:
            by_kind[member.kind].append(out)
    return ChatMembersOut(
        users=by_kind[ChatMemberKind.user],
        agents=by_kind[ChatMemberKind.agent],
        workflows=by_kind[ChatMemberKind.workflow],
    )


def chat_summary(
    db: Session,
    organisation_id: int,
    chat: Chat,
    member_count: int,
    role: ChatMemberRole | None,
    known: dict[int, UserOut] | None = None,
) -> ChatSummary:
    """The organisation's chat as the list shows it, to a user with this `role` in it (None: not
    a member); who made and changed it from `known` when given (see `people`), else read now."""
    if known is None:
        known = people(db, organisation_id, [chat.created_by, chat.updated_by])
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


def chat_out(
    db: Session, store: Session, organisation_id: int, chat: Chat, user_id: int
) -> ChatOut:
    """The organisation's chat (in its store, `store`), as the user sees it."""
    member = user_member(chat, user_id)
    return ChatOut(
        **chat_summary(
            db, organisation_id, chat, len(ordered_members(chat)), member.role if member else None
        ).model_dump(),
        members=members_out(db, store, chat),
    )


LOAD = (selectinload(Chat.members),)


def load(store: Session, chat_id: int) -> Chat:
    """The chat fresh from its store, with its members."""
    store.expire_all()
    return store.scalars(select(Chat).where(Chat.id == chat_id).options(*LOAD)).one()


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


def get_visible_chat(store: Session, user_id: int, chat_id: int) -> Chat:
    """The chat, if it exists for the user: any public chat of their organisation's store, or a
    private one of it they're in."""
    chat = store.scalars(select(Chat).where(Chat.id == chat_id).options(*LOAD)).one_or_none()
    if chat is None or (chat.visibility == ChatVisibility.private and not is_member(chat, user_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return chat


def is_owner(chat: Chat, user_id: int) -> bool:
    """Whether the user holds the chat's owner role."""
    member = user_member(chat, user_id)
    return member is not None and member.role == ChatMemberRole.owner


def lock_chat(store: Session, chat: Chat) -> None:
    """Lock the chat until commit, for a change to it or to what's in it; a 404 if it has been
    deleted since `get_visible_chat` read it: by its owner, or by its owner leaving the
    organisation with nobody else in it (`organisations.hand_over_chats`, which locks it first).
    Under the lock that waits for the deletion and sees the chat gone, where adding a member or a
    message to it would break their foreign key (a 500), and a deletion that comes after waits
    and takes what the change added with the chat.
    `FOR NO KEY UPDATE`: the lock the change to `updated_at` each of them makes takes anyway, only
    sooner, so it doesn't hold up what that wouldn't (a message's foreign key takes a key share)."""
    locked = select(Chat.id).where(Chat.id == chat.id).with_for_update(key_share=True)
    if store.scalar(locked) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def lock_owner(store: Session, chat: Chat) -> int | None:
    """Lock the chat (`lock_chat`), and read who holds its owner role now (None: nobody), for a
    change to its members. The members `get_visible_chat` loaded can be out of date by then: the
    owner leaving the organisation deletes their member row and hands the chat on, or deletes it
    (`organisations.hand_over_chats`, which locks the chat first), and a join, add, removal or
    leaving decided on the owner it loaded would go in after that, leaving a member in a chat
    nobody owns. Under the lock it waits for the hand-over, or the hand-over waits for it and sees
    what it did."""
    lock_chat(store, chat)
    return store.scalar(
        select(ChatMember.user_id).where(
            ChatMember.chat_id == chat.id, ChatMember.role == ChatMemberRole.owner
        )
    )


OWNER_ONLY = "Only the chat's owner can do that."


def get_chat_if_owner(store: Session, user_id: int, chat_id: int) -> Chat:
    """The chat, for one of the owner's actions: a 403 for anyone else it's visible to."""
    chat = get_visible_chat(store, user_id, chat_id)
    if not is_owner(chat, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWNER_ONLY)
    return chat


def still_owner(store: Session, chat: Chat, user_id: int) -> None:
    """A 403 unless the user still holds the chat's owner role, read under its lock
    (`lock_owner`), for an owner's change to its members: their leaving the organisation may
    have got in since `get_chat_if_owner`."""
    if lock_owner(store, chat) != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWNER_ONLY)
