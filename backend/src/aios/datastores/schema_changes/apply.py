"""A request's life on the store: reading its schema, proposing a change (`propose`), applying
an approved one on the urgent worker (`apply`), and the outcome the run is resumed with."""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import psycopg
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios import approval_rules, notifications, outbox
from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.models import AgentRun, SchemaChangeRequest
from aios.datastores.resolve import approvers
from aios.db import SessionLocal
from aios.models import DataStore, EventType, SchemaChangeStatus

if TYPE_CHECKING:
    from aios.datastores.provision import StoreNames
# Read on the package at call time, where tests replace them.
from aios.datastores import schema_changes as package
from aios.datastores.schema_changes.compile import _quote, compile_sql, confirmation
from aios.datastores.schema_changes.expiry import _notification_ref
from aios.datastores.schema_changes.spec import (
    DROPPED_PREFIX,
    SCHEMA,
    AddColumn,
    CreateTable,
    DropColumn,
    DropTable,
    Invalid,
    SchemaChange,
    StoreSchema,
    check,
    summarize,
)

log = logging.getLogger(__package__)


# --- The store's schema -------------------------------------------------------------------------

# Every relation in `data` (tables, indexes, sequences, views...), with a table's columns.
_SCHEMA_SQL = """
    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
           coalesce(a.attnum = ANY(pk.indkey), false)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        AND c.relkind IN ('r', 'p')
    LEFT JOIN pg_index pk ON pk.indrelid = c.oid AND pk.indisprimary
    WHERE n.nspname = 'data'
    ORDER BY c.relname, a.attnum
"""
# And types of their own there, whose names a new table's would clash with.
_TYPES_SQL = """
    SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'data' AND t.typrelid = 0
"""
_EMBEDDINGS_SQL = "SELECT table_name, column_name, source_column FROM aios.embedding_columns"


def read_schema(pg: psycopg.Connection) -> StoreSchema:
    """The store's `data` schema, as specs are checked against it."""
    schema = StoreSchema()
    for relation, column, type_, not_null, in_key in pg.execute(_SCHEMA_SQL):
        schema.relations.add(relation)
        if column is None or relation.startswith(DROPPED_PREFIX):
            continue
        if column.startswith(DROPPED_PREFIX):
            schema.dropped_columns.setdefault(relation, set()).add(column)
            continue
        # pgvector's type, named with its schema unless that's on the search path.
        schema.tables.setdefault(relation, {})[column] = type_.removeprefix("extensions.")
        if not_null:
            schema.not_null.setdefault(relation, set()).add(column)
        if in_key:
            schema.primary_keys.setdefault(relation, []).append(column)
    schema.relations.update(name for (name,) in pg.execute(_TYPES_SQL))
    schema.embeddings = {(t, c): source for t, c, source in pg.execute(_EMBEDDINGS_SQL)}
    return schema


def read_revision(pg: psycopg.Connection) -> int:
    """The store's data schema revision: its latest change's (0 before any). Each applied
    change appends to `aios.schema_history`, which bumps it."""
    return pg.execute("SELECT coalesce(max(id), 0) FROM aios.schema_history").fetchone()[0]


# --- Requests -----------------------------------------------------------------------------------


def lock_run(store: Session, run_ids: list[int]) -> None:
    """Lock these runs of the store (in id order) until `store`'s transaction ends, for deleting
    them: recording a request (`propose`) holds its run's key share while it does, so the two take
    turns, and a request is never recorded for a run that's gone: deleting the run then cancels
    the requests it sees (`aios.api.agents.cancel_chat_requests`)."""
    if run_ids:
        store.execute(
            select(AgentRun.id)
            .where(AgentRun.id.in_(run_ids))
            .order_by(AgentRun.id)
            .with_for_update()
        )


def find(data_store_id: uuid.UUID, run_id: int, tool_call_id: str) -> SchemaChangeRequest | None:
    """The request a tool call of the store's run made, if it made one."""
    with store_session(data_store_id, "ro") as store:
        return store.scalar(
            select(SchemaChangeRequest).where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.tool_call_id == tool_call_id,
            )
        )


def propose(
    data_store_id: uuid.UUID,
    change: SchemaChange,
    reason: str,
    *,
    run_id: int | None,
    tool_call_id: str | None,
) -> SchemaChangeRequest:
    """Check `change` against the store's schema and record it as a pending request in the
    store; then notify those who can decide it, on the platform. Raises `Invalid` if the schema
    doesn't take it, and as `store_session` does if the store can't be read."""
    with store_session(data_store_id, "ro") as session:
        pg = session.connection().connection.driver_connection
        # The revision first: a change committed in between makes the schema newer than the
        # revision, which only means it's checked again when it's applied.
        revision = read_revision(pg)
        schema = package.read_schema(pg)
    # The names drops rename to are the platform's to pick, never the model's.
    drops = (DropTable, DropColumn)
    fresh = [
        op.model_copy(update={"dropped_as": None}) if isinstance(op, drops) else op
        for op in change.ops
    ]
    checked = check(SchemaChange(ops=fresh), schema)
    request = SchemaChangeRequest(
        ops=checked.model_dump(mode="json", exclude_none=True)["ops"],
        sql=compile_sql(checked),
        summary=summarize(checked),
        reason=reason,
        requested_by_run=run_id,
        tool_call_id=tool_call_id,
        base_revision=revision,
    )
    with store_session(data_store_id) as store:
        if run_id is not None:
            # Its run's key share, until the request commits: not for a run deleted meanwhile,
            # whose requests have been cancelled already (`lock_run`).
            held = store.scalar(
                select(AgentRun.id)
                .where(AgentRun.id == run_id)
                .with_for_update(read=True, key_share=True)
            )
            if held is None:
                raise Invalid("The run has been deleted")
        store.add(request)
        store.commit()
        least = approval_rules.needed_for(store, request.ops)
    with SessionLocal() as db:
        data_store = db.get(DataStore, data_store_id)
        if data_store is not None and data_store.owner_organisation_id is not None:
            notifications.notify(
                db,
                approvers(db, data_store, least),
                data_store.owner_organisation_id,
                notifications.SCHEMA_CHANGE_REQUEST,
                _notification_ref(data_store_id, request.id),
            )
            db.commit()
    return request


def _store_error(exc: psycopg.Error) -> str:
    """Why applying failed, for the members and the model: Postgres's own message, but not one
    from connecting (which names the server)."""
    if exc.sqlstate and exc.diag.message_primary and not exc.sqlstate.startswith("08"):
        return exc.diag.message_primary
    return "The data store couldn't be reached"


class _Stale(Exception):
    pass


def _run(names: "StoreNames", request: SchemaChangeRequest) -> None:
    """Run the request's SQL on the store, as its owner, all or nothing; raises `_Stale` if the
    schema has moved on and no longer takes it."""
    from aios.datastores import dropped, ownership
    from aios.datastores.provision import as_owner

    with as_owner(names) as conn, schema_transaction(conn):
        done = conn.execute(
            "SELECT 1 FROM aios.schema_history WHERE request_id = %s", (request.id,)
        ).fetchone()
        if done:  # applied already, by a run of this that stopped before recording it
            return
        spec = SchemaChange.model_validate({"ops": request.ops})
        if read_revision(conn) != request.base_revision:
            try:
                rechecked = check(spec, package.read_schema(conn))
            except Invalid as exc:
                raise _Stale(str(exc)) from exc
            # What it found to drop, say, has changed: the SQL approved isn't what it takes now.
            if compile_sql(rechecked) != request.sql:
                raise _Stale(
                    "the tables it changes have changed since it was asked for, so the SQL"
                    " approved isn't what it would take now"
                )
        conn.execute(request.sql)  # several statements: sent whole, with no parameters
        # Owners follow: a new table's is the agent that asked, a dropped one's goes with it.
        for op in spec.ops:
            if isinstance(op, CreateTable):
                ownership.created(conn, op.name, request.requested_by_run)
            elif isinstance(op, DropTable):
                ownership.renamed(conn, op.table, op.dropped_as)
        for table in _access_changed(spec):
            if table_exists(conn, table):
                dropped.grant_access(conn, names, table)
        record_history(conn, request.summary, request.sql, request.id)
        # Its event, with the change (`aios.outbox`): caused by the run that asked for it.
        outbox.emit_pg(
            conn,
            EventType.schema_change_applied,
            {"request_id": str(request.id), "tables": _tables(spec)},
            run_id=request.requested_by_run,
        )


@contextmanager
def schema_transaction(conn: psycopg.Connection) -> Iterator[None]:
    """A transaction (as the store's owner) that changes its schema: applying a request, or
    restoring or purging what was dropped. It waits for each lock no longer than
    `SCHEMA_CHANGE_LOCK_TIMEOUT`, so it can't stall the store's other queries for long, and
    holds `aios.schema_history`'s, so changes run one at a time, each seeing the last (the
    revision can't move until it commits)."""
    with conn.transaction():
        timeout = f"{get_settings().schema_change_lock_timeout}ms"
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (timeout,))
        conn.execute("LOCK TABLE aios.schema_history IN EXCLUSIVE MODE")
        yield


def record_history(
    conn: psycopg.Connection, summary: str, sql: str, request_id: uuid.UUID | None = None
) -> None:
    """Log a change in `aios.schema_history` (in its `schema_transaction`), which bumps the
    store's data schema revision."""
    conn.execute(
        "INSERT INTO aios.schema_history (request_id, summary, sql) VALUES (%s, %s, %s)",
        (request_id, summary, sql),
    )


def _tables(change: SchemaChange) -> list[str]:
    """The tables a change makes or changes, as it names them, in order."""
    tables = [getattr(op, "table", None) or getattr(op, "name", None) for op in change.ops]
    return list(dict.fromkeys(t for t in tables if t))


def _access_changed(change: SchemaChange) -> list[str]:
    """The tables (as they're named once it's applied) whose roles' access a change sets anew
    (`dropped.grant_access`): those it makes (written only by their owner, once it's recorded),
    drops, drops columns from, or adds columns to (which may have dropped columns, so get their
    privileges column by column)."""
    tables: list[str] = []
    for op in change.ops:
        if isinstance(op, CreateTable):
            tables.append(op.name)
        elif isinstance(op, DropTable):
            tables.append(op.dropped_as)
        elif isinstance(op, (DropColumn, AddColumn)):
            tables.append(op.table)
    return list(dict.fromkeys(tables))


def table_exists(pg: psycopg.Connection, table: str) -> bool:
    """Whether `data.<table>` is there."""
    qualified = f"{_quote(SCHEMA)}.{_quote(table)}"
    return pg.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,)).fetchone()[0]


def apply(data_store_id: uuid.UUID, request_id: uuid.UUID) -> SchemaChangeStatus | None:
    """Apply an approved request to its store and record how it went (`applied`, `failed` or
    `stale`); returns that, or None if the request isn't approved (there's nothing to do)."""
    from aios.datastores.provision import StoreNames, provisioner_locked

    with store_session(data_store_id, "ro") as store:
        request = store.get(SchemaChangeRequest, request_id)
        if request is None or request.status != SchemaChangeStatus.approved:
            return None
    with SessionLocal() as db:
        names = StoreNames.of(db.get_one(DataStore, data_store_id))
    error = None
    try:
        # Not while the store is being migrated or provisioned.
        with provisioner_locked(data_store_id):
            _run(names, request)
        status = SchemaChangeStatus.applied
        outbox.pending(data_store_id)
    except _Stale as exc:
        status, error = SchemaChangeStatus.stale, str(exc)
    except psycopg.Error as exc:
        status, error = SchemaChangeStatus.failed, _store_error(exc)
    except Exception:
        log.exception("Applying schema change request %s failed", request_id)
        status, error = SchemaChangeStatus.failed, "Applying it failed unexpectedly"
    with store_session(data_store_id) as store:
        store.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.id == request_id,
                SchemaChangeRequest.status == SchemaChangeStatus.approved,
            )
            .values(
                status=status,
                error=error,
                applied_at=func.now() if status == SchemaChangeStatus.applied else None,
            )
        )
        store.commit()
    return status


def outcome(request: SchemaChangeRequest) -> dict[str, Any]:
    """What the run that asked is resumed with, and its tool call returns to the model."""
    status = request.status
    out: dict[str, Any] = {"status": status.value, "request_id": str(request.id)}
    if status == SchemaChangeStatus.applied:
        out["message"] = (
            f"The user approved it, and it's applied: {request.summary}. `describe_data`"
            " shows the store's tables as they are now."
        )
        if confirmation(SchemaChange.model_validate({"ops": request.ops})):
            out["message"] += (
                " What it dropped is kept for a while: the user can restore it from the Data"
                " page until it's purged."
            )
    elif status == SchemaChangeStatus.rejected:
        out["note"] = request.note
        out["message"] = "The user rejected it, and nothing changed" + (
            f". Their note: {request.note}" if request.note else "."
        )
    elif status == SchemaChangeStatus.stale:
        out["error"] = request.error
        out["message"] = (
            "The store's schema changed after the request was made, and it no longer applies,"
            f" so nothing changed: {request.error}"
        )
    elif status == SchemaChangeStatus.failed:
        out["error"] = request.error
        out["message"] = (
            f"The user approved it, but applying it failed, and nothing changed: {request.error}"
        )
    elif status == SchemaChangeStatus.expired:
        out["message"] = (
            "Nobody decided on the request in time, so it expired, and nothing changed. Treat it"
            " as rejected; ask again if the change is still needed."
        )
    else:  # cancelled
        out["message"] = "The request was cancelled without being applied: nothing changed."
    return out
