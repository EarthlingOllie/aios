"""The stream relay: server-sent events streams of an open chat and of a person, relaying what
the stream publisher (`aios.streams`) publishes over Redis.

- **A chat's stream** (`GET /api/chats/{id}/stream`): anyone in the organisation opens any chat's,
  joined or not (a 404 for anyone else). It carries the chat's `message`, `invocation`, `draft`,
  `chat` and `members` events, `gone` and `ping`.
- **A person's stream** (`GET /api/me/stream`), across every organisation they're in: their
  `chat_activity`, `chat_list` and `notification` events, and `ping`.

**Watching.** A chat's stream opened with `visible` (the client's tab shows it) marks the person
as watching the chat while it's open (`aios.streams.watch`): replies in it don't notify them.
The client opens it again, without or with `visible`, when its tab is hidden or shown.

**Catching up.** A chat's stream subscribes first, buffering what's published, then reads the
messages after the last event id (the `Last-Event-ID` header or `last_event_id`), the chat's
unfinished invocations and any current drafts, and sends those, then what it buffered and what
comes after, dropping message ids it has sent: none is missed, and none comes twice.

**Closing.** Leaving the organisation closes the person's streams of it with `gone` and its
reason (leaving a chat doesn't: anyone can read it). Past `streams_per_person` streams of a person
in a process, the oldest is closed as `gone: replaced`. Past `streams_per_process`, a new stream is
a 503 with a retry hint; so is any while Redis can't be reached, and a stream whose Redis
connection fails ends (a reconnect then gets the 503, and the client reads every few seconds
until a stream opens again). Heartbeats (`ping`) go every `stream_heartbeat_seconds`.
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import redis
import redis.asyncio as aioredis
from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from aios import streams
from aios.api import chats as chats_api
from aios.auth import CurrentDataStore, CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Chat, ChatMessage
from aios.db import DB, DbId, SessionLocal
from aios.models import TriggerTargetKind
from aios.permissions import Action, allow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

UNAVAILABLE = "Live updates aren't available right now. Try again shortly."
# The most messages a chat's stream catches up with when it opens.
CATCH_UP_LIMIT = 1000
# Headers that keep proxies from buffering or transforming a stream.
STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}

Event = tuple[str, Any, int | None]


class _Stream:
    """An open stream in this process, whose relay closes it when told (`close`)."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.reason: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed: asyncio.Event | None = None

    def bind(self) -> asyncio.Event:
        """Bind to the relay's event loop: the event set when it's to close."""
        self._loop = asyncio.get_running_loop()
        self._closed = asyncio.Event()
        if self.reason is not None:
            self._closed.set()
        return self._closed

    def close(self, reason: str) -> None:
        """Close it with `gone` and `reason`, from any thread."""
        self.reason = reason
        if self._loop is not None and self._closed is not None:
            self._loop.call_soon_threadsafe(self._closed.set)


class _Streams:
    """The streams open in this process, held to `streams_per_process` and `streams_per_person`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: list[_Stream] = []

    def admit(self, user_id: int) -> _Stream | None:
        """A new stream of the person's; None past the process's cap. Past the person's, their
        oldest closes as replaced."""
        settings = get_settings()
        with self._lock:
            if len(self._open) >= settings.streams_per_process:
                return None
            stream = _Stream(user_id)
            self._open.append(stream)
            theirs = [s for s in self._open if s.user_id == user_id and s.reason is None]
            over = theirs[: max(0, len(theirs) - settings.streams_per_person)]
        for old in over:
            old.close(streams.REPLACED)
        return stream

    def release(self, stream: _Stream) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._open.remove(stream)

    def count(self) -> int:
        with self._lock:
            return len(self._open)


OPEN = _Streams()


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    """One server-sent event."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        UNAVAILABLE,
        headers={"Retry-After": str(get_settings().stream_retry_seconds)},
    )


def _check_redis() -> None:
    """A 503 with a retry hint if Redis can't be reached."""
    try:
        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        with client:
            client.ping()
    except (redis.RedisError, OSError):
        raise _unavailable() from None


def _open(user_id: int) -> _Stream:
    _check_redis()
    stream = OPEN.admit(user_id)
    if stream is None:
        raise _unavailable()
    return stream


async def _relay(
    stream: _Stream,
    channels: list[str],
    relay: Callable[[dict[str, Any]], list[Event] | None],
    catch_up: Callable[[], list[Event]] | None = None,
    alive: Callable[[], None] | None = None,
    ended: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """The stream's events: subscribed to `channels` first, then what `catch_up` reads, then
    what's published, each as `relay` turns it into events (None: close with `gone`), message
    ids sent once; a heartbeat every `stream_heartbeat_seconds`. `alive` is called once it's
    open and at each heartbeat, `ended` once it has closed."""
    settings = get_settings()
    closed = stream.bind()
    client = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
    pubsub = client.pubsub()
    waiting_closed = asyncio.ensure_future(closed.wait())
    next_message: asyncio.Future | None = None
    sent: set[int] = set()

    def out(events: list[Event]) -> str:
        chunks = []
        for event, data, event_id in events:
            if event == "message" and event_id is not None:
                if event_id in sent:
                    continue
                sent.add(event_id)
            chunks.append(sse(event, data, event_id))
        return "".join(chunks)

    try:
        try:
            await pubsub.subscribe(*channels)
        except (redis.RedisError, OSError) as exc:
            log.warning("a stream couldn't subscribe: %s", exc)
            return
        if alive is not None:
            await run_in_threadpool(alive)
        yield f"retry: {settings.stream_retry_seconds * 1000}\n\n"
        if catch_up is not None and (chunk := out(await run_in_threadpool(catch_up))):
            yield chunk
        heartbeat = settings.stream_heartbeat_seconds
        next_ping = time.monotonic() + heartbeat
        while True:
            if next_message is None:
                next_message = asyncio.ensure_future(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
                )
            done, _ = await asyncio.wait(
                {next_message, waiting_closed},
                timeout=max(0.0, next_ping - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiting_closed in done:
                yield sse("gone", {"reason": stream.reason})
                return
            if next_message in done:
                found, next_message = next_message, None
                try:
                    message = found.result()
                except (redis.RedisError, OSError) as exc:
                    # Redis went away: the stream ends, and the client reconnects.
                    log.warning("a stream lost Redis: %s", exc)
                    return
                if message is None or message.get("type") != "message":
                    continue
                events = relay(json.loads(message["data"]))
                if events is None:
                    return
                if chunk := out(events):
                    yield chunk
            if time.monotonic() >= next_ping:
                yield sse("ping", {})
                next_ping = time.monotonic() + heartbeat
                if alive is not None:
                    await run_in_threadpool(alive)
    finally:
        OPEN.release(stream)
        if ended is not None:
            # Here and now: a closing stream's task may not get to await anything more.
            with contextlib.suppress(Exception):
                ended()
        waiting_closed.cancel()
        if next_message is not None:
            next_message.cancel()
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await client.aclose()


def _streaming(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers=STREAM_HEADERS)


def _drafts(store_id: uuid.UUID, chat_id: int, invocation_ids: list[int]) -> list[Event]:
    """The chat's current drafts of these invocations' replies."""
    if not invocation_ids:
        return []
    try:
        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        with client:
            texts = client.mget([streams.draft_key(store_id, chat_id, i) for i in invocation_ids])
    except (redis.RedisError, OSError):
        return []
    return [
        ("draft", {"invocation_id": i, "text": t.decode()}, None)
        for i, t in zip(invocation_ids, texts, strict=True)
        if t
    ]


def chat_catch_up(
    store_id: uuid.UUID, organisation_id: int, chat_id: int, after: int | None
) -> list[Event]:
    """What a chat's stream sends as it opens: the messages after `after` (none without it),
    the invocations they belong to and the chat's unfinished ones, and current drafts."""
    with SessionLocal() as db, store_session(store_id, "ro") as store:
        rows: list[ChatMessage] = []
        if after is not None:
            rows = list(
                store.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == chat_id, ChatMessage.id > after)
                    .order_by(ChatMessage.id)
                    .limit(CATCH_UP_LIMIT)
                )
            )
        out = chats_api.messages_out(db, store, organisation_id, chat_id, rows)
    events: list[Event] = [("message", m.model_dump(mode="json"), m.id) for m in out.messages]
    events += [("invocation", i.model_dump(mode="json"), None) for i in out.invocations]
    live = [
        i.id
        for i in out.invocations
        if i.target_kind == TriggerTargetKind.agent and i.finished_at is None
    ]
    return events + _drafts(store_id, chat_id, live)


def _last_event_id(query: int | None, header: str | None) -> int | None:
    if query is not None:
        return query
    if header is not None and header.strip().isdecimal():
        return int(header.strip())
    return None


@router.get("/chats/{chat_id}/stream", dependencies=[allow(Action.read)])
def chat_stream(
    chat_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
    last_event_id: Annotated[int | None, Query(ge=0)] = None,
    last_event_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    visible: bool = False,
):
    """The chat's live events, as server-sent events (see the module docstring): anyone in the
    organisation, joined or not; a 404 for a chat that isn't the organisation's. `visible`: the
    client's tab is showing it, so the person is watching the chat while it's open, and isn't
    notified of replies in it (`aios.streams.watching`); the client opens it again when that
    changes."""
    user_id, organisation_id = user.id, org.id
    with store_session(store_id, "ro") as store:
        if store.scalar(select(Chat.id).where(Chat.id == chat_id)) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
    after = _last_event_id(last_event_id, last_event_header)
    stream = _open(user_id)

    def relay(message: dict[str, Any]) -> list[Event] | None:
        event, data = message["event"], message["data"]
        if event == "gone":
            # The person's own channel: only their leaving this organisation closes it.
            if data.get("organisation_id") != organisation_id:
                return []
            stream.close(data["reason"])
            return []
        if event in ("chat_activity", "chat_list", "notification"):
            return []
        return [(event, data, message.get("id"))]

    token = uuid.uuid4().hex
    return _streaming(
        _relay(
            stream,
            [streams.chat_channel(store_id, chat_id), streams.person_channel(user_id)],
            relay,
            lambda: chat_catch_up(store_id, organisation_id, chat_id, after),
            alive=(lambda: streams.watch(store_id, chat_id, user_id, token)) if visible else None,
            ended=(lambda: streams.unwatch(store_id, chat_id, user_id, token)) if visible else None,
        )
    )


@router.get("/me/stream")
def person_stream(user: CurrentUser, db: DB):
    """The person's live chat list and notification events, across every organisation they're
    in, as server-sent events (see the module docstring)."""
    user_id = user.id
    # Nothing more is read here: its connection goes back to the pool for the stream's life.
    db.close()
    stream = _open(user_id)

    def relay(message: dict[str, Any]) -> list[Event] | None:
        event, data = message["event"], message["data"]
        if event == "gone":
            # Their other organisations' chats are still theirs: the list hears it left this one.
            return [("chat_list", {**data, "change": data["reason"]}, None)]
        if event in ("chat_activity", "chat_list", "notification"):
            return [(event, data, None)]
        return []

    return _streaming(_relay(stream, [streams.person_channel(user_id)], relay))
