"""Chats: listing, creating, reading and updating them, and archiving and unarchiving them.
Nobody deletes a chat."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session, aliased

from aios import assistant, audit, guards, organisations, streams
from aios.api.agents.common import lock_live_agent
from aios.api.chats import rules
from aios.api.chats.common import (
    CHAT_NAME_INDEX,
    ChatCreateIn,
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
from aios.datastores.models import Agent, Chat, ChatMember, ChatMessage
from aios.db import DB, DbId
from aios.models import ChatMemberKind, TriggerTargetKind
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api")

# The transaction-level advisory lock naming a chat for an agent takes, in the organisation's
# store (`agent_chat_name`).
NAMING_LOCK = 0x61696F73_63686174  # "aioschat"
# Room for a " (n)" after a generated name, within the 255 a chat's name may have.
NAME_BASE_MAX = 245


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
    body: ChatCreateIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Make a chat of the organisation's, open to all of it, with the user as its first member,
    and the organisation's documents assistant (`aios.assistant`) as its first agent. A 404 for a
    user who left the organisation as they made it (`organisations.holds_member`, held until the
    store has committed the chat): they'd be left in a chat of an organisation they're not in.

    **Chat with this agent** (`agent_id`): the chat includes that agent only, not the documents
    assistant, and without a name is named for it (`agent_chat_name`). It needs permission to
    invoke the agent (its **invocable by**: a 403 saying who may), and the agent not archived
    (a 409); an agent the organisation hasn't got is a 404."""
    if not organisations.holds_member(db, org, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if body.agent_id is None:
        agent = assistant.documents_assistant(store, user.id)
    else:
        agent = store.get(Agent, body.agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown agent")
        refused = guards.refusals(store, role, [(TriggerTargetKind.agent, agent.id)])
        if refused:
            raise HTTPException(status.HTTP_403_FORBIDDEN, next(iter(refused.values())))
        # Share-locked until the chat commits, so archiving it meanwhile waits or wins.
        lock_live_agent(store, agent, read=True)
    chat = Chat(
        **built_by(user.id),
        name=body.name or agent_chat_name(store, agent.name, user.full_name),
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
        store.flush()
        streams.chat_changed(store, chat.id, streams.APPEARED)
        store.commit()
    db.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)


def agent_chat_name(store: Session, agent: str, person: str) -> str:
    """The name "<agent> – <person> – <date>" (today, UTC), or the first of it with " (2)",
    " (3)"… that none of the organisation's chats (archived ones too) is called. Holds a lock,
    for the transaction, that two chats named this way at once both take, so they don't pick
    one name."""
    store.execute(select(func.pg_advisory_xact_lock(NAMING_LOCK)))
    base = f"{agent} – {person} – {datetime.now(UTC).date().isoformat()}"[:NAME_BASE_MAX]
    taken = set(store.scalars(select(Chat.name).where(Chat.name.startswith(base))))
    n = 1
    while (name := base if n == 1 else f"{base} ({n})") in taken:
        n += 1
    return name


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
    streams.chat_changed(store, chat.id, streams.RENAMED)
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
        streams.chat_changed(store, chat.id, streams.ARCHIVED)
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
        streams.chat_changed(store, chat.id, streams.UNARCHIVED)
        store.commit()
    return chat_out(db, store, org.id, load(store, chat.id), user.id, role)
