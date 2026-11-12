"""The outbox: what happened in an organisation's store, for event triggers to react to
(`aios.datastores.models.Event`; the dispatcher, `aios.dispatcher`, reacts).

- **Written with the change.** Each change that emits an event writes it in its own
  transaction (`emit`), so a change that rolls back writes none, and none is lost:
  - a document becoming ready (`aios.ingest`): document ingested;
  - a `write_data` call (`aios.tools.write_data`): table written;
  - an approved schema change applied (`aios.datastores.schema_changes.apply`): schema change
    applied;
  - a message posted in a chat, by a person, by the post-message tool or as a run's result
    (`aios.api.chats.messages`, `aios.tools.post_message`, `aios.invocations`): chat message,
    with its text, for keyword filters;
  - a call a webhook trigger's endpoint accepted (`aios.api.webhooks`): webhook received, for
    that trigger alone; a delivery a webhook source stored, clean (`aios.push_sources`):
    webhook received, for the triggers listening to that source.
- **Filters.** Each event type's triggers filter on its fields (`FILTERS`, checked by
  `checked_filter`, matched by `matches`): tables by name without `data.`, keywords as whole
  words ignoring case (`keyword_pattern`).
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
import re
import uuid
from dataclasses import dataclass
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
            "text": message.body,
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


def batch_document(events: list[Event], dropped: int) -> dict[str, Any]:
    """A debounce batch as a run is given it: its events (`event_document`), in order, and how
    many more matched past the cap."""
    return {
        "type": events[-1].type.value,
        "events": [event_document(e) for e in events],
        "count": len(events),
        "dropped": dropped,
    }


def default_input(document: dict[str, Any]) -> str:
    """A workflow run's input for an event's document with no template: a webhook's body, as
    the caller sent it (which the workflow's input schema checks), else the whole as JSON."""
    if document.get("type") == EventType.webhook_received.value and "payload" in document:
        return json.dumps(document["payload"].get("body"))
    return json.dumps(document)


def agent_message(trigger: Trigger, document: dict[str, Any]) -> str:
    """What an event trigger's agent run is told: the trigger's `input`, if any, then the event
    (`event_document`) or debounce batch (`batch_document`), wrapped as untrusted (it names
    what people, agents and callers outside made)."""
    wrapped = jev.wrap(json.dumps(document, indent=2), "event")
    return f"{trigger.input.strip()}\n\n{wrapped}" if trigger.input.strip() else wrapped


@dataclass(frozen=True)
class FilterField:
    """A field an event type's triggers may filter on: a list of values of `kind` (any of which
    matches), or with `single` one value; strings trimmed and lower-cased, and with `choices`
    one of those; `required` for every trigger of the type."""

    kind: type
    choices: tuple[str, ...] | None = None
    single: bool = False
    required: bool = False
    pattern: str | None = None


MENTION = r"(user|agent|workflow):[1-9][0-9]*"

# What each event type's triggers may filter on, by filter field.
FILTERS: dict[EventType, dict[str, FilterField]] = {
    EventType.document_ingested: {
        "sources": FilterField(int),
        "content_types": FilterField(str),
    },
    # Tables by name, with or without `data.`.
    EventType.table_written: {
        "tables": FilterField(str),
        "operations": FilterField(str, choices=("insert", "upsert", "update", "delete")),
    },
    EventType.schema_change_applied: {"tables": FilterField(str)},
    # The chat it listens to (any of the organisation's); who wrote it; whom it mentions
    # ("agent:3"); and words in it, matched whole and ignoring case.
    EventType.chat_message: {
        "chat_id": FilterField(int, single=True, required=True),
        "author_kinds": FilterField(str, choices=("user", "agent", "workflow", "system")),
        "mentions": FilterField(str, pattern=MENTION),
        "keywords": FilterField(str),
    },
    # A trigger with sources listens to those webhook sources' deliveries rather than having an
    # endpoint of its own.
    EventType.webhook_received: {"sources": FilterField(int)},
}

# What an event trigger fires on, in plain English.
DESCRIPTIONS: dict[EventType, str] = {
    EventType.document_ingested: "When a document is ingested",
    EventType.table_written: "When a data table is written",
    EventType.schema_change_applied: "When a schema change is applied",
    EventType.chat_message: "When a message is posted in a chat",
    EventType.webhook_received: "When its webhook is called",
}

# The event types a trigger may be made for.
TRIGGERABLE = tuple(EventType)


class BadFilter(ValueError):
    pass


def _checked_value(key: str, field: FilterField, value: Any) -> Any:
    if not isinstance(value, field.kind) or isinstance(value, bool):
        raise BadFilter(f"Filter field {key!r} takes {field.kind.__name__}s.")
    if field.kind is str:
        value = value.strip().lower()
        if key == "tables":
            value = table_name(value)
        if value and field.choices and value not in field.choices:
            raise BadFilter(f"Filter field {key!r} takes one of: {', '.join(field.choices)}.")
        if value and field.pattern and not re.fullmatch(field.pattern, value):
            raise BadFilter(f"Filter field {key!r} has a bad value {value!r}.")
    return value


def checked_filter(type: EventType, given: dict[str, Any]) -> dict[str, Any]:
    """The filter for an event type's trigger: each field a list of values of its type, unique,
    in order (or one value, for a `single` field); empty lists left out. Raises `BadFilter` for
    an unknown field, a wrong value, or a required field missing."""
    fields = FILTERS[type]
    out: dict[str, Any] = {}
    for key, values in given.items():
        if key not in fields:
            known = ", ".join(sorted(fields)) or "none"
            raise BadFilter(f"Unknown filter field {key!r} (known: {known}).")
        field = fields[key]
        if field.single:
            if values is not None:
                out[key] = _checked_value(key, field, values)
            continue
        if not isinstance(values, list):
            raise BadFilter(f"Filter field {key!r} takes a list of {field.kind.__name__}s.")
        checked = [_checked_value(key, field, v) for v in values]
        checked = [v for v in checked if v != ""]
        if checked:
            out[key] = list(dict.fromkeys(checked))
    for key, field in fields.items():
        if field.required and key not in out:
            raise BadFilter(f"Filter field {key!r} is required.")
    return out


def table_name(name: str) -> str:
    """A data table's name as filters compare it: without `data.`, lower case."""
    return name.strip().lower().removeprefix("data.")


def keyword_pattern(keyword: str) -> re.Pattern:
    """A keyword as a whole word (or phrase), ignoring case: "outage" doesn't match
    "outages"."""
    return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)


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
    if type in (EventType.table_written, EventType.schema_change_applied):
        tables = filter.get("tables")
        written = (
            [payload.get("table") or ""]
            if type == EventType.table_written
            else payload.get("tables") or []
        )
        if tables and not {table_name(t) for t in written} & set(tables):
            return False
        operations = filter.get("operations")
        if operations and payload.get("operation") not in operations:
            return False
    elif type == EventType.chat_message:
        if filter.get("chat_id") is not None and payload.get("chat_id") != filter["chat_id"]:
            return False
        kinds = filter.get("author_kinds")
        if kinds and payload.get("author_kind") not in kinds:
            return False
        mentions = filter.get("mentions")
        mentioned = {f"{m.get('kind')}:{m.get('id')}" for m in payload.get("mentions") or []}
        if mentions and not mentioned & set(mentions):
            return False
        keywords = filter.get("keywords")
        text = payload.get("text") or ""
        if keywords and not any(keyword_pattern(k).search(text) for k in keywords):
            return False
    elif type == EventType.webhook_received and "source_id" in payload:
        if payload["source_id"] not in (filter.get("sources") or []):
            return False
    elif type == EventType.document_ingested:
        sources = filter.get("sources")
        if sources and payload.get("source_id") not in sources:
            return False
        types = filter.get("content_types")
        if types and not any(_content_type_matches(t, payload.get("content_type")) for t in types):
            return False
    return True
