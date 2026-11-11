"""Chats: listing, creating, reading, updating and deleting them."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import aliased

from aios import assistant, organisations
from aios.api.chats.common import (
    CHAT_NAME_INDEX,
    ChatIn,
    ChatOut,
    ChatSummary,
    chat_out,
    chat_summary,
    get_chat_if_owner,
    get_visible_chat,
    load,
    lock_chat,
)
from aios.api.provenance import made_by, people
from aios.api.skills import unique_name
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Chat,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatMessage,
    ChatVisibility,
)

router = APIRouter(prefix="/api")


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
        select(func.count(ChatMember.id)).where(ChatMember.chat_id == Chat.id).scalar_subquery()
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
    known = people(db, org.id, [i for row in rows for i in (row[0].created_by, row[0].updated_by)])
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
    organisation's documents assistant (`aios.assistant`) as its first agent. A 404 for a user
    who left the organisation as they made it (`organisations.holds_member`): it would be owned by
    someone it doesn't exist for."""
    # It locks the organisation, which `holds_member` would otherwise lock first.
    agent = assistant.documents_assistant(db, org, user.id)
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
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
            agent=agent,
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


@router.put("/chats/{chat_id}", response_model=ChatOut)
def update_chat(chat_id: DbId, body: ChatIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Rename, describe or change the visibility of the chat (its owner only)."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    # Its owner leaving the organisation may be deleting it (`lock_chat`).
    lock_chat(db, chat)
    chat.name = body.name
    chat.description = body.description
    chat.visibility = body.visibility
    chat.updated_by = user.id
    with unique_name(db, "a chat", CHAT_NAME_INDEX):
        db.commit()
    return chat_out(db, load(db, chat.id), user.id)


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB) -> None:
    """Delete the chat and its members (its owner only). A statement, its members, messages and
    invocations going with it in the database, not the ORM's delete of each member it loaded: one
    leaving the organisation meanwhile has deleted their row already (`take_out`)."""
    chat = get_chat_if_owner(db, org, user.id, chat_id)
    db.execute(delete(Chat).where(Chat.id == chat.id))
    db.commit()
