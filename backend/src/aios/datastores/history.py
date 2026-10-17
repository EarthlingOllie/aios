"""The run history export: a copy of a user's agent runs in their data store's `history` schema.

The platform database stays the source of truth; the copy is one-way, and only while the
store's owner has it switched on (`User.export_run_history`, on the Settings page):

- **Switching it on** queues the store's set-up on the provisioning worker
  (`enqueue_set_up`), which brings the store tree's `history` line to its head
  (`aios.datastores.provision`), and backfills: every run of the owner's goes in the outbox.
- **The outbox** is `history_exports`, a row per run and store. Whatever changes a run (a turn
  ending, `aios.api.agents._end_turn`, which every chat, schedule and workflow agent node turn
  goes through; or deleting it) writes its row with `enqueue_runs`, in the same transaction as
  its own change, and the store's export is queued once that commits. Nothing else does, so a
  copy's agent name and schedule are as of its run's last turn: renaming the agent or deleting
  the schedule shows in the copy at the run's next turn, not before.
- **The export** (`export`, the `aios.export_history` task, on the app workers: it writes as
  the store's `rw` role) drains the store's outbox, copying each run and its messages,
  flattened from its checkpoint, or deleting the copy of a run that's gone. Copies are upserted
  by the platform's ids, so exporting again changes nothing. A run that fails to export keeps
  its error on its row and waits for its next change, or `RETRY_AFTER`, so it doesn't hold up
  the rest. A periodic sweep (`stores_to_export`) queues stores whose rows are still pending,
  should a queued export be lost.
- **Switching it off** stops the export, keeping what was copied; `delete_exported` empties it
  (and, while the export is on, starts it over). Runs deleted meanwhile still go in the
  outbox, so their copies go once it's back on.
"""

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import ColumnElement, and_, delete, event, func, literal, or_, select, update
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

# How long a run that failed to export waits before it's tried again (seconds), unless it changes
# first: what failed may pass (a statement timeout, a dropped connection), or may not.
RETRY_AFTER = 60 * 60

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


def enqueue_runs(db: Session, *where: ColumnElement[bool], deleting: bool = False) -> None:
    """Put the agent runs matching `where` in their stores' outbox, if their owner has the
    export on; in `db`'s transaction, which the caller commits (the stores' exports are queued
    then). A run already there is pending again.

    For runs being deleted, call it before deleting them, with `deleting`: they're found
    through their rows. Their copies are kept while the export is off, so they go in the outbox
    then too, if the store has its `history` tables, and are deleted once it's back on."""
    exporting: ColumnElement[bool] = User.export_run_history
    if deleting:
        exporting = or_(exporting, DataStore.history_revision.is_not(None))
    runs = (
        select(
            DataStore.id, literal(HistoryExportKind.agent_run, HistoryExport.kind.type), AgentRun.id
        )
        .join(Agent, Agent.id == AgentRun.agent_id)
        .join(User, User.id == Agent.owner_id)
        .join(DataStore, DataStore.owner_user_id == User.id)
        .where(exporting, *where)
        # Rows are locked in run order, as the export marks them, so two writers of several
        # don't each hold one the other wants.
        .order_by(AgentRun.id)
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
            set_={
                "enqueued_at": func.clock_timestamp(),
                "exported_at": None,
                "error": None,
                "retry_at": None,
            },
        ).returning(HistoryExport.data_store_id)
    ).all()
    if stores and deleting:
        # Only the exporting ones are queued: the others' rows wait for it to be back on.
        stores = db.scalars(
            select(DataStore.id)
            .join(User, User.id == DataStore.owner_user_id)
            .where(DataStore.id.in_(stores), User.export_run_history)
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
        while True:
            locked = conn.scalar(select(func.pg_try_advisory_lock(key)))
            # The lock is the session's: no transaction need stay open (idle) while it's held.
            conn.commit()
            if locked or time.monotonic() >= deadline:
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


def _pending() -> ColumnElement[bool]:
    """Outbox rows waiting to be exported: not yet, and not set aside by an error until later."""
    return and_(
        HistoryExport.exported_at.is_(None),
        or_(HistoryExport.retry_at.is_(None), HistoryExport.retry_at <= func.now()),
    )


def _export_batch(data_store_id: uuid.UUID) -> int:
    """Export the store's oldest pending runs; how many there were."""
    with SessionLocal() as db:
        pending = db.execute(
            select(HistoryExport.id, HistoryExport.source_id, HistoryExport.enqueued_at)
            .where(
                HistoryExport.data_store_id == data_store_id,
                _pending(),
            )
            .order_by(HistoryExport.enqueued_at)
            .limit(BATCH)
        ).all()
    if not pending:
        return 0
    runs = _load_runs([p.source_id for p in pending])
    # A run that fails (its checkpoint won't load, its copy won't write) is set aside with its
    # error, until it changes again or RETRY_AFTER, so the runs after it still go in.
    errors: dict[int, str] = {}

    def failed(p, exc: Exception) -> None:
        log.exception("Couldn't export agent run %s to data store %s", p.source_id, data_store_id)
        errors[p.id] = f"{type(exc).__name__}: {exc}"

    # Every thread read first, on one connection, so the store's transaction isn't left idle.
    messages: dict[int, list[dict[str, Any]]] = {}
    with checkpoints.sync_saver() as saver:
        for p in pending:
            if p.source_id in runs:
                try:
                    messages[p.source_id] = flatten(
                        p.source_id, checkpoints.history(p.source_id, saver)
                    )
                except Exception as exc:
                    failed(p, exc)
    with store_session(data_store_id, "rw") as store:
        for p in pending:
            run = runs.get(p.source_id)
            if run is None:
                # Deleted (its messages go with it, by their foreign key).
                store.execute(delete(HistoryAgentRun).where(HistoryAgentRun.id == p.source_id))
            elif p.id not in errors:
                try:
                    with store.begin_nested():
                        _write_run(store, run, messages[p.source_id])
                except Exception as exc:
                    failed(p, exc)
        store.commit()
    with SessionLocal() as db:
        for p in sorted(pending, key=lambda p: p.source_id):  # in run order, as enqueue_runs
            # Only as it was read: a run changed since is pending again, for the next batch.
            same = (HistoryExport.id == p.id, HistoryExport.enqueued_at == p.enqueued_at)
            if p.id in errors:
                db.execute(
                    update(HistoryExport)
                    .where(*same)
                    .values(
                        error=errors[p.id],
                        retry_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, RETRY_AFTER),
                    )
                )
            elif p.source_id in runs:
                db.execute(
                    update(HistoryExport)
                    .where(*same)
                    .values(exported_at=func.now(), error=None, retry_at=None)
                )
            else:
                db.execute(delete(HistoryExport).where(*same))
        db.commit()
    return len(pending)


def export(data_store_id: uuid.UUID) -> int:
    """Drain the store's outbox into its `history` tables; returns how many runs it exported.
    Does nothing unless the store is exporting (see `_exporting`): its rows wait; switched off
    while it runs, it stops after the batch under way. Nor while another export (or delete) of
    the store runs: what that one leaves pending, the periodic sweep queues again.

    Raises `DataStoreBusy` if the store has no connections free."""
    exported = 0
    with _locked(data_store_id) as locked:
        if not locked:
            return 0
        while True:
            with SessionLocal() as db:
                if not _exporting(db, data_store_id):
                    return exported
            count = _export_batch(data_store_id)
            exported += count
            if count < BATCH:
                break
        # Caught up, even with nothing to export: the Settings page waits for this.
        with SessionLocal() as db:
            db.execute(
                update(DataStore)
                .where(DataStore.id == data_store_id)
                .values(history_exported_at=func.now())
            )
            db.commit()
    return exported


def stores_to_export() -> list[uuid.UUID]:
    """Exporting stores with runs still pending, or that have never exported (for the
    periodic sweep): one set up as it was provisioned has had no export queued."""
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(DataStore.id)
                .join(User, User.id == DataStore.owner_user_id)
                .where(
                    User.export_run_history,
                    DataStore.status == DataStoreStatus.ready,
                    DataStore.history_revision.is_not(None),
                    or_(
                        select(HistoryExport.id)
                        .where(
                            HistoryExport.data_store_id == DataStore.id,
                            _pending(),
                        )
                        .exists(),
                        DataStore.history_exported_at.is_(None),
                    ),
                )
            )
        )


def delete_exported(data_store_id: uuid.UUID) -> None:
    """Empty the store's `history` tables (if it has them). If the export is on, it starts
    over: every run of the owner's goes in the outbox again, so the whole copy comes back
    rather than only runs that change. To keep it empty, switch the export off first.

    Raises as `store_session` does, and `DataStoreBusy` if an export of the store is still
    running after `LOCK_WAIT` (a backfill can take minutes)."""
    with _locked(data_store_id, LOCK_WAIT) as locked:
        if not locked:
            raise DataStoreBusy(data_store_id)
        with SessionLocal() as db:
            store = db.get(DataStore, data_store_id)
            owner_id = store.owner_user_id if store and store.history_revision else None
        if owner_id is None:
            return
        with store_session(data_store_id, "rw") as store:
            store.execute(delete(HistoryMessage))
            store.execute(delete(HistoryAgentRun))
            store.commit()
    # Once the lock is let go: the export this queues may start at once, and would find it held.
    with SessionLocal() as db:
        db.execute(
            update(DataStore).where(DataStore.id == data_store_id).values(history_exported_at=None)
        )
        enqueue_runs(db, Agent.owner_id == owner_id)
        db.commit()


def set_exporting(db: Session, user_id: int, on: bool) -> uuid.UUID | None:
    """Switch the user's export on or off, in `db`'s transaction, which the caller commits.

    Switching it on (from off) backfills: every run of theirs goes in the outbox, and the store
    hasn't caught up until they're exported. It returns their store, to queue its set-up once
    committed (`enqueue_set_up`)."""
    # Locked, so two switches at once don't both backfill, or both miss it.
    was_on = db.scalar(select(User.export_run_history).where(User.id == user_id).with_for_update())
    db.execute(update(User).where(User.id == user_id).values(export_run_history=on))
    if not on or was_on:
        return None
    from aios.datastores.resolve import data_store_for

    store_id = data_store_for(user_id, db)
    enqueue_runs(db, Agent.owner_id == user_id)
    db.execute(update(DataStore).where(DataStore.id == store_id).values(history_exported_at=None))
    return store_id
