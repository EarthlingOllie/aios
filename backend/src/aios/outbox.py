"""The outbox: what happened in an organisation's store, for event triggers to react to
(`aios.datastores.models.Event`; the dispatcher, `aios.dispatcher`, reacts).

- **Written with the change.** Each change that emits an event writes it in its own
  transaction (`emit`), so a change that rolls back writes none, and none is lost:
  - a document becoming ready (`aios.ingest`): document ingested;
  - a `write_data` call (`aios.tools.write_data`): table written;
  - an approved schema change applied (`aios.datastores.schema_changes.apply`): schema change
    applied;
  - a message posted in a chat, by a person or as a run's result (`aios.api.chats.messages`,
    `aios.invocations`): chat message.
- **Its causes.** An event names the invocation whose run caused it, if one did, and carries
  that invocation's chain of causes, root first, then the invocation: what the loop rules look
  at (`aios.dispatcher`). A person's own action has none.
- **Pending.** Once the store has committed events, the writer stamps the store's registry row
  (`DataStore.events_pending_at`) and publishes a wake-up (`pending`), which bring the
  dispatcher to it. A session that emitted does this as it commits (a listener); a raw
  connection's caller calls `pending` itself. Both are best effort: the sweep
  (`aios.dispatcher.sweep`) stamps a store whose stamp was lost.
"""

import json
import logging
import uuid
from typing import Any

import psycopg
import redis
from psycopg.types.json import Jsonb
from sqlalchemy import event as sa_event
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from aios import jev
from aios.config import get_settings
from aios.datastores.connections import store_id_of
from aios.datastores.models import ChatMessage, Event, Trigger
from aios.db import SessionLocal
from aios.models import DataStore, EventType

log = logging.getLogger(__name__)

WAKE_CHANNEL = "aios:events:wake"
_PENDING = "aios.events_pending"


def _causes(
    pg: psycopg.Connection, invocation_id: int | None, run_id: int | None
) -> tuple[int | None, list[int]]:
    """The causing invocation (given, or the agent run's latest) and its chain, root first."""
    if invocation_id is not None:
        row = pg.execute(
            "SELECT id, ancestor_ids FROM aios.invocations WHERE id = %s", (invocation_id,)
        ).fetchone()
    elif run_id is not None:
        row = pg.execute(
            "SELECT id, ancestor_ids FROM aios.invocations WHERE agent_run_id = %s"
            " ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    else:
        row = None
    if row is None:
        return None, []
    return row[0], [*row[1], row[0]]


def emit_pg(
    pg: psycopg.Connection,
    type: EventType,
    payload: dict[str, Any],
    *,
    invocation_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Write an event in the connection's transaction, caused by the invocation, or the agent
    run's (or neither: a person's own action); its id. The caller calls `pending` once it has
    committed."""
    cause, chain = _causes(pg, invocation_id, run_id)
    (event_id,) = pg.execute(
        "INSERT INTO aios.events (type, payload, invocation_id, chain)"
        " VALUES (%s::aios.event_type, %s, %s, %s) RETURNING id",
        (type.value, Jsonb(payload), cause, chain),
    ).fetchone()
    return event_id


def emit(
    store: Session,
    type: EventType,
    payload: dict[str, Any],
    *,
    invocation_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """`emit_pg` in the store session's transaction; the session stamps the store pending as
    it commits."""
    store.flush()
    pg: psycopg.Connection = store.connection().connection.driver_connection
    event_id = emit_pg(pg, type, payload, invocation_id=invocation_id, run_id=run_id)
    store.info[_PENDING] = True
    return event_id


def message_posted(
    store: Session, message: ChatMessage, *, invocation_id: int | None = None
) -> None:
    """The chat message event of a message just added to a chat (by a person, or as the result
    of `invocation_id`'s run), in the store session's transaction."""
    store.flush()
    emit(
        store,
        EventType.chat_message,
        {
            "chat_id": message.chat_id,
            "message_id": message.id,
            "author_kind": message.author_kind.value,
            "author_id": message.author_user_id
            or message.author_agent_id
            or message.author_workflow_id,
            "mentions": [{"kind": m["kind"], "id": m["id"]} for m in message.mentions or []],
        },
        invocation_id=invocation_id,
    )


def pending(data_store_id: uuid.UUID) -> None:
    """Stamp the store as having events the dispatcher hasn't seen, and wake the dispatcher.
    Best effort: the sweep finds a store whose stamp was lost."""
    try:
        with SessionLocal() as db:
            db.execute(
                update(DataStore)
                .where(DataStore.id == data_store_id)
                .values(events_pending_at=func.now())
            )
            db.commit()
    except Exception:
        log.exception("couldn't stamp data store %s's events pending", data_store_id)
        return
    wake()


def wake() -> None:
    """Wake the dispatcher (`aios.dispatcher.run_forever`). Best effort: it polls too."""
    try:
        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        with client:
            client.publish(WAKE_CHANNEL, "1")
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't wake the event dispatcher: %s", exc)


@sa_event.listens_for(Session, "after_commit")
def _stamp_after_commit(session: Session) -> None:
    if session.info.pop(_PENDING, False):
        pending(store_id_of(session))


@sa_event.listens_for(Session, "after_rollback")
def _forget_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


def event_document(event: Event) -> dict[str, Any]:
    """The event as a run is given it."""
    return {
        "id": event.id,
        "type": event.type.value,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "payload": event.payload,
    }


def agent_message(trigger: Trigger, event: Event) -> str:
    """What an event trigger's agent run is told: the trigger's `input`, if any, then the event,
    wrapped as untrusted (it names what people and agents made)."""
    wrapped = jev.wrap(json.dumps(event_document(event), indent=2), "event")
    return f"{trigger.input.strip()}\n\n{wrapped}" if trigger.input.strip() else wrapped


# What each event type's triggers may filter on: filter field -> the type of its value.
FILTERS: dict[EventType, dict[str, type]] = {
    EventType.document_ingested: {"sources": int, "content_types": str},
    EventType.table_written: {},
    EventType.schema_change_applied: {},
    EventType.chat_message: {},
}

# What an event trigger fires on, in plain English.
DESCRIPTIONS: dict[EventType, str] = {
    EventType.document_ingested: "When a document is ingested",
    EventType.table_written: "When a data table is written",
    EventType.schema_change_applied: "When a schema change is applied",
    EventType.chat_message: "When a message is posted in a chat",
}

# The event types a trigger may be made for so far.
TRIGGERABLE = (EventType.document_ingested,)


class BadFilter(ValueError):
    pass


def checked_filter(type: EventType, given: dict[str, Any]) -> dict[str, list]:
    """The filter for an event type's trigger: each field a list of values of its type, unique,
    in order; empty lists left out. Raises `BadFilter` for an unknown field or a wrong value."""
    fields = FILTERS[type]
    out: dict[str, list] = {}
    for key, values in given.items():
        if key not in fields:
            known = ", ".join(sorted(fields)) or "none"
            raise BadFilter(f"Unknown filter field {key!r} (known: {known}).")
        kind = fields[key]
        if not isinstance(values, list) or not all(
            isinstance(v, kind) and not isinstance(v, bool) for v in values
        ):
            raise BadFilter(f"Filter field {key!r} takes a list of {kind.__name__}s.")
        if kind is str:
            values = [v.strip().lower() for v in values if v.strip()]
        if values:
            out[key] = list(dict.fromkeys(values))
    return out


def _content_type_matches(pattern: str, content_type: str | None) -> bool:
    if not content_type:
        return False
    content_type = content_type.split(";")[0].strip().lower()
    if pattern.endswith("/*"):
        return content_type.startswith(pattern[:-1])
    return content_type == pattern


def matches(type: EventType, filter: dict[str, list], payload: dict[str, Any]) -> bool:
    """Whether an event of this type, with this payload, passes a trigger's filter: every field
    given, any of its values."""
    if type == EventType.document_ingested:
        sources = filter.get("sources")
        if sources and payload.get("source_id") not in sources:
            return False
        types = filter.get("content_types")
        if types and not any(_content_type_matches(t, payload.get("content_type")) for t in types):
            return False
    return True
