"""The run history export: a copy of a user's agent runs in their data store's `history` schema.

The platform database stays the source of truth; the copy is one-way, and only while the
store's owner has it switched on (`User.export_run_history`, on the Settings page):

- **Switching it on** queues the store's set-up on the provisioning worker
  (`enqueue_set_up`), which brings the store tree's `history` line to its head
  (`aios.datastores.provision`), and backfills: every run of the owner's goes in the outbox.
- **The outbox** is `history_exports`, a row per run and store. Whatever changes a run (a turn
  ending, `aios.api.agents._end_turn`, which every chat, schedule and workflow agent node turn
  goes through; or deleting it) writes its row with `enqueue_runs`, in the same transaction as
  its own change, and the store's export is queued once that commits.
- **The export** (`export`, the `aios.export_history` task, on the app workers: it writes as
  the store's `rw` role) drains the store's outbox, copying each run and its messages,
  flattened from its checkpoint, or deleting the copy of a run that's gone. Copies are upserted
  by the platform's ids, so exporting again changes nothing. A run that fails to export keeps
  its error on its row and waits for its next change, so it doesn't hold up the rest. A
  periodic sweep (`stores_to_export`) queues stores whose rows are still pending, should a
  queued export be lost.
- **Switching it off** stops the export, keeping what was copied; `delete_exported` empties it.
"""

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import ColumnElement, delete, event, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import checkpoints
from aios.datastores.connections import DataStoreBusy, store_session
from aios.datastores.models import HistoryAgentRun, HistoryMessage
from aios.db import SessionLocal, engine
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    DataStore,
    DataStoreStatus,
    HistoryExport,
    HistoryExportKind,
    User,
    WorkflowRunStep,
)

log = logging.getLogger(__name__)

# Runs exported per round trip to the store.
BATCH = 50

# Messages per insert: well inside Postgres's limit on a statement's parameters.
MESSAGES_PER_INSERT = 1000

# How long deleting a store's exported history waits for an export of it to finish (seconds).
LOCK_WAIT = 5

# Stores a session queued runs of, to export once it commits (see `_export_committed`).
_TO_EXPORT = "aios.history_exports"


def enqueue_export(data_store_id: uuid.UUID) -> None:
    """Queue the store's export on the app workers."""
    from aios.worker import export_history_task

    export_history_task.delay(str(data_store_id))


def enqueue_set_up(data_store_id: uuid.UUID) -> None:
    """Queue the store's set-up (its `history` tables) on the provisioning worker."""
    from aios.worker import set_up_history_export_task

    set_up_history_export_task.delay(str(data_store_id))


@event.listens_for(Session, "after_commit")
def _export_committed(session: Session) -> None:
    for store_id in session.info.pop(_TO_EXPORT, ()):
        try:
            enqueue_export(store_id)
        except Exception:
            # The rows are committed and stay pending: the periodic sweep queues the export.
            log.exception("Couldn't queue the history export of data store %s", store_id)


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session: Session) -> None:
    session.info.pop(_TO_EXPORT, None)


def enqueue_runs(db: Session, *where: ColumnElement[bool]) -> None:
    """Put the agent runs matching `where` in their stores' outbox, if their owner has the
    export on; in `db`'s transaction, which the caller commits (the stores' exports are queued
    then). A run already there is pending again.

    For a run being deleted, call it before deleting the run: it's found through its row."""
    runs = (
        select(
            DataStore.id, literal(HistoryExportKind.agent_run, HistoryExport.kind.type), AgentRun.id
        )
        .join(Agent, Agent.id == AgentRun.agent_id)
        .join(User, User.id == Agent.owner_id)
        .join(DataStore, DataStore.owner_user_id == User.id)
        .where(User.export_run_history, *where)
    )
    stmt = insert(HistoryExport).from_select(
        [HistoryExport.data_store_id, HistoryExport.kind, HistoryExport.source_id], runs
    )
    stores = db.scalars(
        stmt.on_conflict_do_update(
            index_elements=[
                HistoryExport.data_store_id,
                HistoryExport.kind,
                HistoryExport.source_id,
            ],
            set_={"enqueued_at": func.clock_timestamp(), "exported_at": None, "error": None},
        ).returning(HistoryExport.data_store_id)
    ).all()
    if stores:
        db.info.setdefault(_TO_EXPORT, set()).update(stores)


@contextmanager
def _locked(data_store_id: uuid.UUID, wait: float = 0) -> Iterator[bool]:
    """Holds the store's export lock, if it's free or frees up within `wait` seconds; whether it
    got it. One export (or delete) of a store at a time, so a run's copy is never written by
    two at once, in either order. Never waiting long, so exports queued while a long one runs
    (a backfill) don't hold the app workers, and ingest with them."""
    with engine.connect() as conn:
        key = func.hashtextextended(f"aios.history_export:{data_store_id}", 0)
        deadline = time.monotonic() + wait
        while not (locked := conn.scalar(select(func.pg_try_advisory_lock(key)))):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        try:
            yield bool(locked)
        finally:
            if locked:
                conn.execute(select(func.pg_advisory_unlock(key)))
            conn.commit()


def _exporting(db: Session, data_store_id: uuid.UUID) -> bool:
    """The store is ready, has its `history` tables, and its owner has the export on."""
    return bool(
        db.scalar(
            select(User.export_run_history)
            .join(DataStore, DataStore.owner_user_id == User.id)
            .where(
                DataStore.id == data_store_id,
                DataStore.status == DataStoreStatus.ready,
                DataStore.history_revision.is_not(None),
            )
        )
    )


def _clean(value: Any) -> Any:
    """Postgres's text and JSON can't hold NUL characters, which model output can have."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


ROLES = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}


def flatten(run_id: int, messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The run's messages as `history.messages` rows: role, text, tool calls and usage."""
    rows = []
    for position, message in enumerate(messages):
        dumped = message.model_dump(mode="json")
        row: dict[str, Any] = {
            "agent_run_id": run_id,
            "position": position,
            "message_id": message.id,
            "role": ROLES.get(message.type, message.type),
            "text": message.text,
            "tool_calls": None,
            "tool_call_id": None,
            "tool_name": None,
            "is_error": None,
            "usage": None,
            "model": None,
        }
        if isinstance(message, AIMessage):
            row["tool_calls"] = [
                {"id": c["id"], "name": c["name"], "args": c["args"]}
                for c in dumped.get("tool_calls") or []
            ] or None
            row["usage"] = dumped.get("usage_metadata")
            row["model"] = (message.response_metadata or {}).get("model_name")
        elif isinstance(message, ToolMessage):
            row["tool_call_id"] = message.tool_call_id
            row["tool_name"] = message.name
            row["is_error"] = message.status == "error"
        rows.append(_clean(row))
    return rows


def _load_runs(run_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The runs that still exist, as `history.agent_runs` rows."""
    workflow_run = (
        select(WorkflowRunStep.run_id)
        .where(WorkflowRunStep.agent_run_id == AgentRun.id)
        .limit(1)
        .scalar_subquery()
    )
    with SessionLocal() as db:
        rows = db.execute(
            select(
                AgentRun.id,
                AgentRun.agent_id,
                Agent.name.label("agent_name"),
                AgentVersion.version.label("agent_version"),
                AgentVersion.model,
                AgentRun.title,
                # Who ran it: today, an agent's runs are its owner's.
                Agent.owner_id.label("user_id"),
                AgentRun.trigger_id,
                workflow_run.label("workflow_run_id"),
                AgentRun.message_count,
                AgentRun.created_at,
                AgentRun.updated_at,
            )
            .join(Agent, Agent.id == AgentRun.agent_id)
            .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
            .where(AgentRun.id.in_(run_ids))
        ).mappings()
        return {r["id"]: _clean(dict(r)) for r in rows}


def _write_run(store: Session, run: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    stmt = insert(HistoryAgentRun).values(**run, exported_at=func.now())
    store.execute(
        stmt.on_conflict_do_update(
            index_elements=[HistoryAgentRun.id],
            set_={c: stmt.excluded[c] for c in [*run, "exported_at"] if c != "id"},
        )
    )
    for start in range(0, len(messages), MESSAGES_PER_INSERT):
        stmt = insert(HistoryMessage).values(messages[start : start + MESSAGES_PER_INSERT])
        store.execute(
            stmt.on_conflict_do_update(
                index_elements=[HistoryMessage.agent_run_id, HistoryMessage.position],
                set_={
                    c: stmt.excluded[c]
                    for c in messages[0]
                    if c not in ("agent_run_id", "position")
                },
            )
        )
    # A thread only grows, but a copy from before can be longer than the run: e.g. deleted
    # exported history isn't, and a run's thread can be recorded again.
    store.execute(
        delete(HistoryMessage).where(
            HistoryMessage.agent_run_id == run["id"], HistoryMessage.position >= len(messages)
        )
    )


def _export_batch(data_store_id: uuid.UUID) -> int:
    """Export the store's oldest pending runs; how many there were."""
    with SessionLocal() as db:
        pending = db.execute(
            select(HistoryExport.id, HistoryExport.source_id, HistoryExport.enqueued_at)
            .where(
                HistoryExport.data_store_id == data_store_id,
                HistoryExport.exported_at.is_(None),
                HistoryExport.error.is_(None),
            )
            .order_by(HistoryExport.enqueued_at)
            .limit(BATCH)
        ).all()
    if not pending:
        return 0
    runs = _load_runs([p.source_id for p in pending])
    errors: dict[int, str] = {}
    with store_session(data_store_id, "rw") as store:
        for p in pending:
            run = runs.get(p.source_id)
            if run is None:
                # Deleted (its messages go with it, by their foreign key).
                store.execute(delete(HistoryAgentRun).where(HistoryAgentRun.id == p.source_id))
                continue
            try:
                with store.begin_nested():
                    _write_run(store, run, flatten(run["id"], checkpoints.history(run["id"])))
            except Exception as exc:
                # E.g. its checkpoint won't load. Set aside with its error (until the run
                # changes again), so the runs after it still go in.
                log.exception(
                    "Couldn't export agent run %s to data store %s", p.source_id, data_store_id
                )
                errors[p.id] = f"{type(exc).__name__}: {exc}"
        store.commit()
    with SessionLocal() as db:
        for p in pending:
            # Only as it was read: a run changed since is pending again, for the next batch.
            same = (HistoryExport.id == p.id, HistoryExport.enqueued_at == p.enqueued_at)
            if p.id in errors:
                db.execute(update(HistoryExport).where(*same).values(error=errors[p.id]))
            elif p.source_id in runs:
                db.execute(update(HistoryExport).where(*same).values(exported_at=func.now()))
            else:
                db.execute(delete(HistoryExport).where(*same))
        db.execute(
            update(DataStore)
            .where(DataStore.id == data_store_id)
            .values(history_exported_at=func.now())
        )
        db.commit()
    return len(pending)


def export(data_store_id: uuid.UUID) -> int:
    """Drain the store's outbox into its `history` tables; returns how many runs it exported.
    Does nothing unless the store is exporting (see `_exporting`): its rows wait. Nor while
    another export (or delete) of the store runs: what that one leaves pending, the periodic
    sweep queues again.

    Raises `DataStoreBusy` if the store has no connections free."""
    exported = 0
    with _locked(data_store_id) as locked:
        if not locked:
            return 0
        with SessionLocal() as db:
            if not _exporting(db, data_store_id):
                return 0
        while count := _export_batch(data_store_id):
            exported += count
            if count < BATCH:
                break
    return exported


def stores_to_export() -> list[uuid.UUID]:
    """Exporting stores with runs still pending (for the periodic sweep)."""
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(DataStore.id)
                .join(User, User.id == DataStore.owner_user_id)
                .where(
                    User.export_run_history,
                    DataStore.status == DataStoreStatus.ready,
                    DataStore.history_revision.is_not(None),
                    select(HistoryExport.id)
                    .where(
                        HistoryExport.data_store_id == DataStore.id,
                        HistoryExport.exported_at.is_(None),
                        HistoryExport.error.is_(None),
                    )
                    .exists(),
                )
            )
        )


def delete_exported(data_store_id: uuid.UUID) -> None:
    """Empty the store's `history` tables (if it has them). The outbox is left as it is: runs
    pending still go in, if the export is on.

    Raises as `store_session` does, and `DataStoreBusy` if an export of the store is still
    running after `LOCK_WAIT` (a backfill can take minutes)."""
    with _locked(data_store_id, LOCK_WAIT) as locked:
        if not locked:
            raise DataStoreBusy(data_store_id)
        with SessionLocal() as db:
            store = db.get(DataStore, data_store_id)
            has_history = store is not None and store.history_revision is not None
        if not has_history:
            return
        with store_session(data_store_id, "rw") as store:
            store.execute(delete(HistoryMessage))
            store.execute(delete(HistoryAgentRun))
            store.commit()


def set_exporting(db: Session, user_id: int, on: bool) -> uuid.UUID | None:
    """Switch the user's export on or off, in `db`'s transaction, which the caller commits.

    Switching it on (from off) backfills: every run of theirs goes in the outbox. It returns
    their store, to queue its set-up once committed (`enqueue_set_up`)."""
    # Locked, so two switches at once don't both backfill, or both miss it.
    was_on = db.scalar(select(User.export_run_history).where(User.id == user_id).with_for_update())
    db.execute(update(User).where(User.id == user_id).values(export_run_history=on))
    if not on or was_on:
        return None
    from aios.datastores.resolve import data_store_for

    store_id = data_store_for(user_id, db)
    enqueue_runs(db, Agent.owner_id == user_id)
    return store_id
