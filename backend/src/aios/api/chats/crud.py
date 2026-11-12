"""Chats: listing, creating, reading, updating and deleting them."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.orm import aliased

from aios import assistant, organisations
from aios.api.chats.common import (
    CHAT_NAME_INDEX,
    ChatIn,
    ChatOut,
    ChatSummary,
    chat_out,
    chat_summary,
    get_chat_if_member,
    get_visible_chat,
    load,
    lock_chat,
)
from aios.api.provenance import built_by, people
from aios.api.skills import unique_name
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import Chat, ChatMember, ChatMessage
from aios.db import DB, DbId
from aios.models import ChatMemberKind
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")


class ChatListed(ChatSummary):
    # How many of its messages others posted since the last one the user saw (`POST
    # /api/chats/{id}/read`); null for a chat they haven't joined.
    unread_count: int | None


@router.get("/chats", response_model=list[ChatListed], dependencies=[allow(Action.read)])
def list_chats(user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore):
    """Every chat of the organisation's, joined (`is_member`) or not, the most recently active
    first."""
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
    rows = store.execute(
        select(Chat, member_count, mine.id.is_not(None), unread)
        .outerjoin(
            mine,
            and_(
                mine.chat_id == Chat.id,
                mine.kind == ChatMemberKind.user,
                mine.user_id == user.id,
            ),
        )
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
    ).all()
    known = people(db, org.id, [i for row in rows for i in (row[0].created_by, row[0].updated_by)])
    return [
        ChatListed(
            **chat_summary(db, org.id, chat, count, joined, known=known).model_dump(),
            unread_count=unread_count,
        )
        for chat, count, joined, unread_count in rows
    ]


@router.post(
    "/chats",
    response_model=ChatOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def create_chat(
    body: ChatIn, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Make a chat of the organisation's, open to all of it, with the user as its first member,
    and the organisation's documents assistant (`aios.assistant`) as its first agent. A 404 for a
    user who left the organisation as they made it (`organisations.holds_member`, held until the
    store has committed the chat): they'd be left in a chat of an organisation they're not in."""
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    agent = assistant.documents_assistant(store, user.id)
    chat = Chat(
        **built_by(user.id),
        name=body.name,
        description=body.description,
    )
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.user,
            user_id=user.id,
            added_by=user.id,
        )
    )
    chat.members.append(
        ChatMember(
            kind=ChatMemberKind.agent,
            agent_id=agent.id,
            added_by=user.id,
        )
    )
    store.add(chat)
    with unique_name(store, "a chat", CHAT_NAME_INDEX):
        store.commit()
    db.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id)


@router.get("/chats/{chat_id}", response_model=ChatOut, dependencies=[allow(Action.read)])
def get_chat(
    chat_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    return chat_out(db, store, org.id, get_visible_chat(store, user.id, chat_id), user.id)


@router.put("/chats/{chat_id}", response_model=ChatOut, dependencies=[allow(Action.post)])
def update_chat(
    chat_id: DbId,
    body: ChatIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Rename or describe the chat (any of its members)."""
    chat = get_chat_if_member(store, user.id, chat_id)
    # A member may be deleting it (`lock_chat`).
    lock_chat(store, chat)
    chat.name = body.name
    chat.description = body.description
    chat.updated_by = user.id
    with unique_name(store, "a chat", CHAT_NAME_INDEX):
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id)


@router.delete(
    "/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[allow(Action.post)]
)
def delete_chat(chat_id: DbId, user: CurrentUser, store: CurrentStore) -> None:
    """Delete the chat and its members (any of its members). A statement, its members, messages
    and invocations going with it in the database, not the ORM's delete of each member it loaded:
    one leaving the organisation meanwhile has deleted their row already (`take_out`)."""
    chat = get_chat_if_member(store, user.id, chat_id)
    store.execute(delete(Chat).where(Chat.id == chat.id))
    store.commit()
