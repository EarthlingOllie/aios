"""The stream publisher: live updates for open chats and for people, over Redis pub/sub.

The API and the run service note what they change in a chat as they change it, in the store
session's transaction (`message_added`, `invocation_changed`, `run_changed`, `chat_changed`,
`members_changed`); once that transaction commits, the publisher reads what changed as the
chat's reads return it and publishes it (a session listener). A transaction that rolls back
publishes nothing. The stream relay in the API (`aios.api.streams`) relays what's published to
the streams open in its process.

- **A chat's channel** (`chat_channel`, by store and chat) carries its events: `message` (as the
  messages read returns it, its id the event id), `invocation` (its state, run and all), `draft`
  (the whole reply so far of an agent invocation), `chat` (its header: name, description,
  archived) and `members`.
- **A person's channel** (`person_channel`) carries what keeps their chat list live: `chat
  activity` (a chat's newest message and their unread count) and `chat list` (a chat appeared,
  was joined, left, renamed, archived or unarchived), and `gone` (they left an organisation:
  their streams of it close).
- **Drafts.** While an agent's turn runs, the run service keeps its reply so far in Redis, with
  an expiry (`Draft`), and publishes the whole text at most every
  `draft_publish_interval_seconds`, so a missed event costs nothing. Drafts never reach the
  database: the posted reply replaces the draft, and a failed turn's invocation event says so.

Publishing is best effort: with Redis down nothing is published, and open streams have closed
(clients read periodically until it's back). What's committed is in the database either way.
"""

import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import redis
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores.connections import store_id_of

log = logging.getLogger(__name__)

# Chat list changes a person's stream carries.
APPEARED = "appeared"
JOINED = "joined"
LEFT = "left"
RENAMED = "renamed"
ARCHIVED = "archived"
UNARCHIVED = "unarchived"
# Why a stream closes (`gone`).
LEFT_ORGANISATION = "left_organisation"
REPLACED = "replaced"


def _namespace() -> str:
    """The platform database's name: Redis's channels are shared by every database it serves
    (its numbered databases too), and ids repeat across platforms (test runs side by side, say)."""
    return urlsplit(get_settings().database_url).path.lstrip("/")


def chat_channel(data_store_id: uuid.UUID, chat_id: int) -> str:
    return f"aios:stream:{_namespace()}:chat:{data_store_id}:{chat_id}"


def person_channel(user_id: int) -> str:
    return f"aios:stream:{_namespace()}:person:{user_id}"


def draft_key(data_store_id: uuid.UUID, chat_id: int, invocation_id: int) -> str:
    return f"aios:draft:{data_store_id}:{chat_id}:{invocation_id}"


def _client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
    )


def encode(event: str, data: Any, event_id: int | None = None) -> str:
    """An event as it goes over a channel (and, relayed, as a stream sends it)."""
    return json.dumps({"event": event, "data": data, "id": event_id}, default=str)


def publish(channel: str, event: str, data: Any, event_id: int | None = None) -> None:
    publish_many([(channel, encode(event, data, event_id))])


def publish_many(items: Iterable[tuple[str, str]]) -> None:
    """Publish encoded events in order, in one round trip; best effort (see the module)."""
    items = list(items)
    if not items:
        return
    try:
        with _client() as client:
            pipe = client.pipeline(transaction=False)
            for channel, payload in items:
                pipe.publish(channel, payload)
            pipe.execute()
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't publish %d stream events: %s", len(items), exc)


# --- Noting changes, publishing them once committed ---------------------------------------------

_PENDING = "aios.streams"


@dataclass
class _Pending:
    # Messages added, by chat.
    messages: dict[int, set[int]] = field(default_factory=dict)
    invocations: set[int] = field(default_factory=set)
    workflow_runs: set[int] = field(default_factory=set)
    agent_runs: set[int] = field(default_factory=set)
    turns: set[int] = field(default_factory=set)
    # Chat list changes by chat, in order: (change, the person it's for or None for everyone).
    chats: dict[int, list[tuple[str, int | None]]] = field(default_factory=dict)
    # Chats whose members changed.
    members: set[int] = field(default_factory=set)


def _pending(store: Session) -> _Pending:
    return store.info.setdefault(_PENDING, _Pending())


def message_added(store: Session, chat_id: int, message_id: int) -> None:
    _pending(store).messages.setdefault(chat_id, set()).add(message_id)


def invocation_changed(store: Session, invocation_id: int) -> None:
    _pending(store).invocations.add(invocation_id)


def run_changed(
    store: Session, *, workflow_run_id: int | None = None, agent_run_id: int | None = None
) -> None:
    """A run moved on (a step, its counters): the card of the invocation that started it, if a
    chat follows one."""
    pending = _pending(store)
    if workflow_run_id is not None:
        pending.workflow_runs.add(workflow_run_id)
    if agent_run_id is not None:
        pending.agent_runs.add(agent_run_id)


def turn_changed(store: Session, turn_id: int) -> None:
    """A turn's counters moved on: its run's card."""
    _pending(store).turns.add(turn_id)


def chat_changed(store: Session, chat_id: int, change: str, user_id: int | None = None) -> None:
    """The chat appeared, was renamed or described, archived or unarchived (for everyone in the
    organisation), or the person `user_id` joined or left it."""
    _pending(store).chats.setdefault(chat_id, []).append((change, user_id))
    if change in (JOINED, LEFT):
        _pending(store).members.add(chat_id)


def members_changed(store: Session, chat_id: int) -> None:
    _pending(store).members.add(chat_id)


@sa_event.listens_for(Session, "after_flush")
def _note_flushed(session: Session, _context) -> None:
    """Messages and invocations the ORM wrote: whatever path wrote them, they're published."""
    from aios.datastores.models import ChatMessage, Invocation

    for obj in session.new:
        if isinstance(obj, ChatMessage) and obj.id is not None:
            message_added(session, obj.chat_id, obj.id)
        elif isinstance(obj, Invocation) and obj.id is not None and obj.chat_id is not None:
            invocation_changed(session, obj.id)
    for obj in session.dirty:
        if isinstance(obj, Invocation) and obj.chat_id is not None:
            invocation_changed(session, obj.id)


@sa_event.listens_for(Session, "after_commit")
def _publish_after_commit(session: Session) -> None:
    pending: _Pending | None = session.info.pop(_PENDING, None)
    if pending is None:
        return
    try:
        publish_committed(store_id_of(session), pending)
    except Exception:
        log.exception("couldn't publish a chat's changes")


@sa_event.listens_for(Session, "after_rollback")
def _forget_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


def _organisation(db: Session, data_store_id: uuid.UUID) -> int | None:
    from aios.models import DataStore

    return db.scalar(select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id))


def _organisation_people(db: Session, organisation_id: int) -> list[int]:
    from aios.models import OrganisationMember

    return list(
        db.scalars(
            select(OrganisationMember.user_id).where(
                OrganisationMember.organisation_id == organisation_id
            )
        )
    )


def _listening(channels: list[str]) -> set[str]:
    """Those of the channels some stream is subscribed to now: nothing is read or published for
    the rest. One that subscribes after this reads what was committed as it opens."""
    if not channels:
        return set()
    try:
        with _client() as client:
            counts = client.pubsub_numsub(*channels)
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't ask who's listening: %s", exc)
        return set()
    return {(name.decode() if isinstance(name, bytes) else name) for name, count in counts if count}


def publish_committed(data_store_id: uuid.UUID, pending: "_Pending") -> None:
    """Read what changed, now it's committed, as the chat's reads return it, and publish it to
    the streams listening: messages before the invocations they finish, so a reply replaces its
    draft in place."""
    from aios.api import chats as chats_api
    from aios.datastores import store_session
    from aios.datastores.models import Chat, ChatMessage, Invocation, Turn
    from aios.db import SessionLocal
    from aios.models import ChatMessageKind

    items: list[tuple[str, str]] = []
    with SessionLocal() as db, store_session(data_store_id, "ro") as store:
        organisation_id = _organisation(db, data_store_id)
        if organisation_id is None:
            return
        people = _organisation_people(db, organisation_id)
        # The invocations whose cards changed, and their chats.
        agent_runs = set(pending.agent_runs)
        if pending.turns:
            agent_runs.update(
                store.scalars(select(Turn.agent_run_id).where(Turn.id.in_(pending.turns)))
            )
        cards: dict[int, int] = {}
        if pending.invocations or pending.workflow_runs or agent_runs:
            cards = dict(
                store.execute(
                    select(Invocation.id, Invocation.chat_id).where(
                        Invocation.chat_id.is_not(None),
                        Invocation.id.in_(pending.invocations)
                        | Invocation.workflow_run_id.in_(pending.workflow_runs)
                        | Invocation.agent_run_id.in_(agent_runs),
                    )
                ).all()
            )
        chats = set(pending.messages) | set(cards.values()) | set(pending.chats) | pending.members
        listening = _listening(
            [chat_channel(data_store_id, c) for c in chats] + [person_channel(p) for p in people]
        )
        if not listening:
            return
        watched = {c for c in chats if chat_channel(data_store_id, c) in listening}
        listeners = [p for p in people if person_channel(p) in listening]

        # Messages, and each chat's activity for everyone in the organisation listening.
        for chat_id, ids in pending.messages.items():
            if listeners:
                items += _activity(store, organisation_id, chat_id, listeners)
            if chat_id not in watched:
                continue
            rows = list(
                store.scalars(
                    select(ChatMessage).where(ChatMessage.id.in_(ids)).order_by(ChatMessage.id)
                )
            )
            out = chats_api.messages_out(db, store, organisation_id, chat_id, rows)
            channel = chat_channel(data_store_id, chat_id)
            for message in out.messages:
                items.append(
                    (channel, encode("message", message.model_dump(mode="json"), message.id))
                )
            for invocation in out.invocations:
                if invocation.id in cards or invocation.message_id in ids:
                    cards.pop(invocation.id, None)
                    items.append(
                        (channel, encode("invocation", invocation.model_dump(mode="json")))
                    )
        # Invocations whose state changed, in the chats watched.
        changed = [i for i, chat_id in cards.items() if chat_id in watched]
        if changed:
            found = list(
                store.scalars(
                    select(Invocation).where(Invocation.id.in_(changed)).order_by(Invocation.id)
                )
            )
            acks = dict(
                store.execute(
                    select(ChatMessage.invocation_id, ChatMessage.body).where(
                        ChatMessage.invocation_id.in_(changed),
                        ChatMessage.kind == ChatMessageKind.run_card,
                    )
                ).all()
            )
            outs = chats_api.with_runs(
                db,
                store,
                organisation_id,
                [chats_api.invocation_out(i, acks.get(i.id, "")) for i in found],
            )
            for out in outs:
                items.append(
                    (
                        chat_channel(data_store_id, out.chat_id),
                        encode("invocation", out.model_dump(mode="json")),
                    )
                )
        # Headers, members and chat list changes.
        for chat_id in set(pending.chats) | pending.members:
            changes = pending.chats.get(chat_id, [])
            told = {p for _, user_id in changes for p in ([user_id] if user_id else listeners)}
            if chat_id not in watched and not told & set(listeners):
                continue
            chat = store.scalars(
                select(Chat).where(Chat.id == chat_id).options(*chats_api.LOAD)
            ).one_or_none()
            if chat is None:
                continue
            channel = chat_channel(data_store_id, chat_id)
            if chat_id in watched:
                if any(user_id is None for _, user_id in changes):
                    items.append((channel, encode("chat", _header(chat))))
                if chat_id in pending.members:
                    members = chats_api.members_out(db, store, chat)
                    items.append((channel, encode("members", members.model_dump(mode="json"))))
            for change, user_id in changes:
                data = {
                    "organisation_id": organisation_id,
                    "chat_id": chat_id,
                    "change": change,
                    **_header(chat),
                }
                for person in [user_id] if user_id is not None else listeners:
                    if person in listeners:
                        items.append((person_channel(person), encode("chat_list", data)))
    publish_many(items)


def _header(chat) -> dict[str, Any]:
    """What a chat's `chat` event carries: what everyone sees the same of its header."""
    return {
        "id": chat.id,
        "name": chat.name,
        "description": chat.description,
        "archived_at": chat.archived_at.isoformat() if chat.archived_at else None,
        "archived_by": chat.archived_by,
        "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
    }


def _activity(
    store: Session, organisation_id: int, chat_id: int, people: list[int]
) -> list[tuple[str, str]]:
    """Each person's `chat_activity` of a chat that has a new message: its newest message, and
    their unread count of it (null for those who haven't joined)."""
    from sqlalchemy import func

    from aios.datastores.models import ChatMember, ChatMessage
    from aios.models import ChatAuthorKind, ChatMemberKind

    newest = store.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.chat_id == chat_id))
    members = dict(
        store.execute(
            select(ChatMember.user_id, ChatMember.last_read_message_id).where(
                ChatMember.chat_id == chat_id, ChatMember.kind == ChatMemberKind.user
            )
        ).all()
    )
    items = []
    for person in people:
        unread = None
        if person in members:
            unread = store.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.id > func.coalesce(members[person], 0),
                    ~(
                        (ChatMessage.author_kind == ChatAuthorKind.user)
                        & (ChatMessage.author_user_id == person)
                    ),
                )
            )
        items.append(
            (
                person_channel(person),
                encode(
                    "chat_activity",
                    {
                        "organisation_id": organisation_id,
                        "chat_id": chat_id,
                        "message_id": newest,
                        "unread_count": unread,
                    },
                ),
            )
        )
    return items


def left_organisation(db: Session, organisation_id: int, user_id: int) -> None:
    """The person left the organisation: once `db` commits, their streams of it close (`gone`),
    and their chat list hears of it."""
    db.info.setdefault(_GONE, []).append((organisation_id, user_id))


_GONE = "aios.streams.gone"


@sa_event.listens_for(Session, "after_commit")
def _gone_after_commit(session: Session) -> None:
    for organisation_id, user_id in session.info.pop(_GONE, ()):
        publish(
            person_channel(user_id),
            "gone",
            {"reason": LEFT_ORGANISATION, "organisation_id": organisation_id},
        )


@sa_event.listens_for(Session, "after_rollback")
def _forget_gone(session: Session) -> None:
    session.info.pop(_GONE, None)


# --- Drafts --------------------------------------------------------------------------------------


class Draft:
    """An agent invocation's reply as it's written (`add`), kept in Redis with an expiry and
    published whole (`flush`) at most every `draft_publish_interval_seconds`; `clear` once the
    turn has ended (its reply posted, or its failure)."""

    def __init__(self, data_store_id: uuid.UUID, chat_id: int, invocation_id: int) -> None:
        self.data_store_id = data_store_id
        self.chat_id = chat_id
        self.invocation_id = invocation_id
        self.text = ""
        self._published_at = 0.0
        self._published = ""

    def add(self, delta: str) -> bool:
        """Add to the reply; whether it's time to publish it (`flush`)."""
        self.text += delta
        interval = get_settings().draft_publish_interval_seconds
        return time.monotonic() - self._published_at >= interval

    def flush(self) -> None:
        if not self.text or self.text == self._published:
            return
        self._published_at = time.monotonic()
        self._published = self.text
        settings = get_settings()
        data = {"invocation_id": self.invocation_id, "text": self.text}
        try:
            with _client() as client:
                pipe = client.pipeline(transaction=False)
                pipe.set(
                    draft_key(self.data_store_id, self.chat_id, self.invocation_id),
                    self.text,
                    ex=settings.draft_expiry_seconds,
                )
                pipe.publish(chat_channel(self.data_store_id, self.chat_id), encode("draft", data))
                pipe.execute()
        except (redis.RedisError, OSError) as exc:
            log.warning("couldn't publish a draft: %s", exc)

    def clear(self) -> None:
        clear_draft(self.data_store_id, self.chat_id, self.invocation_id)


def clear_draft(data_store_id: uuid.UUID, chat_id: int, invocation_id: int) -> None:
    try:
        with _client() as client:
            client.delete(draft_key(data_store_id, chat_id, invocation_id))
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't clear a draft: %s", exc)
