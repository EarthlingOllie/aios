"""Chats: listing, creating, reading and updating them, and archiving and unarchiving them.
Nobody deletes a chat."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import aliased

from aios import assistant, audit, organisations
from aios.api.chats import rules
from aios.api.chats.common import (
    CHAT_NAME_INDEX,
    ChatIn,
    ChatOut,
    ChatSummary,
    chat_out,
    chat_summary,
    get_visible_chat,
    load,
    lock_chat,
)
from aios.api.chats.rules import ChatAction
from aios.api.provenance import built_by, people
from aios.api.skills import unique_name
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import Chat, ChatMember, ChatMessage
from aios.db import DB, DbId
from aios.models import ChatMemberKind
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api")


class ChatListed(ChatSummary):
    # How many of its messages others posted since the last one the user saw (`POST
    # /api/chats/{id}/read`); null for a chat they haven't joined.
    unread_count: int | None


@router.get("/chats", response_model=list[ChatListed], dependencies=[allow(Action.read)])
def list_chats(
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
    archived_only: Annotated[bool, Query(alias="archived")] = False,
):
    """Every chat of the organisation's that isn't archived, joined (`is_member`) or not, the
    most recently active first; with `archived`, the archived ones instead."""
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
        .where(Chat.archived_at.is_not(None) if archived_only else Chat.archived_at.is_(None))
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
    ).all()
    known = people(
        db,
        org.id,
        [i for row in rows for i in (row[0].created_by, row[0].updated_by, row[0].archived_by)],
    )
    return [
        ChatListed(
            **chat_summary(db, org.id, chat, count, joined, role, known=known).model_dump(),
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
    body: ChatIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
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
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


@router.get("/chats/{chat_id}", response_model=ChatOut, dependencies=[allow(Action.read)])
def get_chat(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    return chat_out(db, store, org.id, get_visible_chat(store, user.id, chat_id), user.id, role)


@router.put("/chats/{chat_id}", response_model=ChatOut, dependencies=[allow(Action.post)])
def update_chat(
    chat_id: DbId,
    body: ChatIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Rename or describe the chat (any of its members; not an archived one). A rename is in the
    audit log."""
    chat = get_visible_chat(store, user.id, chat_id)
    rules.check(role, chat, user.id, ChatAction.edit)
    # Archiving meanwhile (`lock_chat`); and its name as it is now, under the lock, for the log.
    lock_chat(store, chat, writing=True)
    store.refresh(chat, ["name"])
    if body.name != chat.name:
        audit.record(
            store,
            audit.person(user.id),
            "chat.renamed",
            "chat",
            chat.id,
            {"name": body.name, "previous_name": chat.name},
        )
    chat.name = body.name
    chat.description = body.description
    chat.updated_by = user.id
    with unique_name(store, "a chat", CHAT_NAME_INDEX):
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


@router.post(
    "/chats/{chat_id}/archive",
    response_model=ChatOut,
    dependencies=[allow(Action.archive_chats)],
)
def archive_chat(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Archive the chat (admins): it becomes read-only, still read and searched, and runs already
    under way in it still post their results. In the audit log. Archiving an archived chat
    changes nothing."""
    chat = get_visible_chat(store, user.id, chat_id)
    rules.check(role, chat, user.id, ChatAction.archive)
    # A post, join, edit or add under way finishes first; any after sees it archived.
    lock_chat(store, chat)
    store.refresh(chat)
    if chat.archived_at is None:
        chat.archived_at = func.now()
        chat.archived_by = user.id
        audit.record(
            store, audit.person(user.id), "chat.archived", "chat", chat.id, {"name": chat.name}
        )
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


@router.post(
    "/chats/{chat_id}/unarchive",
    response_model=ChatOut,
    dependencies=[allow(Action.archive_chats)],
)
def unarchive_chat(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Unarchive the chat (admins), so it can be picked up again. In the audit log. Unarchiving
    a chat that isn't archived changes nothing."""
    chat = get_visible_chat(store, user.id, chat_id)
    rules.check(role, chat, user.id, ChatAction.unarchive)
    lock_chat(store, chat)
    store.refresh(chat)
    if chat.archived_at is not None:
        chat.archived_at = None
        chat.archived_by = None
        audit.record(
            store, audit.person(user.id), "chat.unarchived", "chat", chat.id, {"name": chat.name}
        )
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)
