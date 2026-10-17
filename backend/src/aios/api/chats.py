"""Chats: group conversations between users, agents and workflows.

A chat has an owner (the user who made it), a name unique among chats, a description and a
visibility. Its members are users so far (`ChatMember` has room for agents and workflows). A
private chat doesn't exist for a user who isn't in it: every endpoint is a 404 for them. Only the
owner can change a chat, remove its members or delete it: a 403 for anyone else. Messages come
later; until then a chat's last activity is when it was last changed.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from aios.api.skills import on_conflict, unique_name
from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import Chat, ChatMember, ChatMemberKind, ChatMemberRole, ChatVisibility, User

router = APIRouter(prefix="/api")

CHAT_NAME_CONSTRAINT = "chats_name_key"
NAME_TAKEN = "A chat with that name already exists."
USER_MEMBER_CONSTRAINT = "chat_members_chat_id_user_id_key"
ALREADY_A_MEMBER = "Already a member of this chat."


class ChatIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    visibility: ChatVisibility = ChatVisibility.private


class MemberIn(BaseModel):
    kind: ChatMemberKind
    # The user's id. Agents and workflows can't be added yet.
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


class ChatSummary(BaseModel):
    id: int
    name: str
    description: str
    visibility: ChatVisibility
    owner_id: int
    # Whether the signed-in user owns it, so the UI knows to offer the owner's actions.
    is_owner: bool
    member_count: int
    created_at: datetime
    # When it was last active: for now, when it was last changed.
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


def members_out(chat: Chat) -> ChatMembersOut:
    by_kind: dict[ChatMemberKind, list[ChatMemberOut]] = {kind: [] for kind in ChatMemberKind}
    # The owner first; the rest as they were added (ids go up with time).
    for member in sorted(chat.members, key=lambda m: (m.role != ChatMemberRole.owner, m.id)):
        by_kind[member.kind].append(member_out(member))
    return ChatMembersOut(
        users=by_kind[ChatMemberKind.user],
        agents=by_kind[ChatMemberKind.agent],
        workflows=by_kind[ChatMemberKind.workflow],
    )


def chat_summary(chat: Chat, user_id: int, member_count: int) -> ChatSummary:
    return ChatSummary(
        id=chat.id,
        name=chat.name,
        description=chat.description,
        visibility=chat.visibility,
        owner_id=chat.owner_id,
        is_owner=chat.owner_id == user_id,
        member_count=member_count,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def chat_out(chat: Chat, user_id: int) -> ChatOut:
    return ChatOut(
        **chat_summary(chat, user_id, len(chat.members)).model_dump(), members=members_out(chat)
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


def is_member(chat: Chat, user_id: int) -> bool:
    return any(m.kind == ChatMemberKind.user and m.user_id == user_id for m in chat.members)


def get_visible_chat(db: Session, user_id: int, chat_id: int) -> Chat:
    """The chat, if it exists for the user: any public chat, or a private one they're in."""
    chat = db.scalars(select(Chat).where(Chat.id == chat_id).options(*LOAD)).one_or_none()
    if chat is None or (chat.visibility == ChatVisibility.private and not is_member(chat, user_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return chat


def get_owned_chat(db: Session, user_id: int, chat_id: int) -> Chat:
    """The chat, for one of the owner's actions: a 403 for anyone else it's visible to."""
    chat = get_visible_chat(db, user_id, chat_id)
    if chat.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the chat's owner can do that.")
    return chat


@router.get("/chats", response_model=list[ChatSummary])
def list_chats(user: CurrentUser, db: DB):
    """The chats the user is a member of, the most recently active first."""
    rows = db.execute(
        select(Chat, func.count(ChatMember.id))
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(Chat.id.in_(select(ChatMember.chat_id).where(ChatMember.user_id == user.id)))
        .group_by(Chat.id)
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
    )
    return [chat_summary(chat, user.id, count) for chat, count in rows]


@router.post("/chats", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
def create_chat(body: ChatIn, user: CurrentUser, db: DB):
    """Make a chat, with the user as its owner and first member."""
    chat = Chat(
        owner_id=user.id,
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
    db.add(chat)
    with unique_name(db, "a chat", CHAT_NAME_CONSTRAINT, NAME_TAKEN):
        db.commit()
    return chat_out(load(db, chat.id), user.id)


@router.get("/chats/{chat_id}", response_model=ChatOut)
def get_chat(chat_id: DbId, user: CurrentUser, db: DB):
    return chat_out(get_visible_chat(db, user.id, chat_id), user.id)


@router.put("/chats/{chat_id}", response_model=ChatOut)
def update_chat(chat_id: DbId, body: ChatIn, user: CurrentUser, db: DB):
    """Rename, describe or change the visibility of the chat (its owner only)."""
    chat = get_owned_chat(db, user.id, chat_id)
    chat.name = body.name
    chat.description = body.description
    chat.visibility = body.visibility
    with unique_name(db, "a chat", CHAT_NAME_CONSTRAINT, NAME_TAKEN):
        db.commit()
    return chat_out(load(db, chat.id), user.id)


@router.post(
    "/chats/{chat_id}/members", response_model=ChatOut, status_code=status.HTTP_201_CREATED
)
def add_member(chat_id: DbId, body: MemberIn, user: CurrentUser, db: DB):
    """Add a user to the chat (its owner only). Agents and workflows can't be added yet."""
    chat = get_owned_chat(db, user.id, chat_id)
    if body.kind != ChatMemberKind.user:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only users can be added to a chat yet."
        )
    if db.get(User, body.id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown user")
    if is_member(chat, body.id):
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_MEMBER)
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.user,
            user_id=body.id,
            role=ChatMemberRole.member,
            added_by=user.id,
        )
    )
    # Adding a member is activity too.
    chat.updated_at = func.now()
    # The check above can lose a race with another add of the same user; this catches that.
    with on_conflict(db, USER_MEMBER_CONSTRAINT, ALREADY_A_MEMBER):
        db.commit()
    return chat_out(load(db, chat.id), user.id)


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: DbId, user: CurrentUser, db: DB) -> None:
    """Delete the chat and its members (its owner only)."""
    db.delete(get_owned_chat(db, user.id, chat_id))
    db.commit()


@router.delete("/chats/{chat_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(chat_id: DbId, member_id: DbId, user: CurrentUser, db: DB) -> None:
    """Remove a member from the chat (its owner only). The owner stays: a chat always has one."""
    chat = get_owned_chat(db, user.id, chat_id)
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
