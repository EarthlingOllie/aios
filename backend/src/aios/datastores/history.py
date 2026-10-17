"""The run history export: a copy of a user's agent runs and workflow runs in their data
store's `history` schema.

The platform database stays the source of truth; the copy is one-way, and only while the
store's owner has it switched on (`User.export_run_history`, on the Settings page):

- **Switching it on** queues the store's set-up on the provisioning worker
  (`enqueue_set_up`), which brings the store tree's `history` line to its head
  (`aios.datastores.provision`), and backfills: every run of the owner's, of each kind, goes in
  the outbox (`enqueue_everything`).
- **The outbox** is `history_exports`, a row per run (by kind) and store. Whatever changes a
  run writes its row, in the same transaction as its own change, and the store's export is
  queued once that commits:
  - an agent run (`enqueue_runs`): a turn ending (`aios.api.agents._end_turn`, which every
    chat, schedule and workflow agent node turn goes through), or deleting it;
  - a workflow run (`enqueue_workflow_runs`): its end, however it ended
    (`aios.workflows._end_runs`), deleting it, or deleting an agent whose runs its steps held.
    Pausing and resuming a run will write it too, once runs can pause.
  Nothing else does, so a copy's agent or workflow name and schedule are as of its run's last
  change: renaming the agent shows in the copy at the run's next turn, not before.
- **The export** (`export`, the `aios.export_history` task, on the app workers: it writes as
  the store's `rw` role) drains the store's outbox, copying each agent run and its messages,
  flattened from its checkpoint, and each workflow run with its steps at every depth, or
  deleting the copy of a run that's gone. Copies are upserted by the platform's ids, so
  exporting again changes nothing. A run that fails to export keeps its error on its row and
  waits for its next change, or `RETRY_AFTER`, so it doesn't hold up the rest. A periodic
  sweep (`stores_to_export`) queues stores whose rows are still pending, should a queued export
  be lost.
- **Embedding** (`User.embed_run_history`, offered only while the export is on: `set_embedding`)
  makes the copy searchable by meaning (`aios.search.history_search`): once the export has
  caught up, it embeds the messages waiting (`_embed`), those just exported or, once switched
  on, every one there. Switched off, nothing new is embedded and what was is kept.
- **Switching it off** stops the export, keeping what was copied; `delete_exported` empties it
  (and, while the export is on, starts it over). Runs deleted meanwhile still go in the
  outbox, so their copies go once it's back on.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    Text,
    and_,
    bindparam,
    case,
    cast,
    delete,
    event,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import checkpoints
from aios.datastores import migrations
from aios.datastores.connections import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_session,
)
from aios.datastores.models import (
    EMBEDDABLE,
    HistoryAgentRun,
    HistoryMessage,
    HistoryWorkflowRun,
    HistoryWorkflowRunStep,
)
from aios.db import SessionLocal, engine
from aios.embeddings import embed_documents
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    DataStore,
    DataStoreStatus,
    HistoryExport,
    HistoryExportKind,
    User,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)

log = logging.getLogger(__name__)

# Runs exported per round trip to the store.
BATCH = 50

# Rows (messages, steps) per insert: well inside Postgres's limit on a statement's parameters.
ROWS_PER_INSERT = 1000

# How long a run that failed to export waits before it's tried again (seconds), unless it changes
# first: what failed may pass (a statement timeout, a dropped connection), or may not.
RETRY_AFTER = 60 * 60

# Messages embedded per round trip to the embedding API (and to the store).
EMBED_BATCH = 128

# How long an export spends embedding (seconds), well inside its task's time limit: what's left
# waits for the periodic sweep, which queues the store's export again.
EMBED_BUDGET = 5 * 60

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


def _enqueue(db: Session, kind: HistoryExportKind, runs: Select, deleting: bool) -> None:
    """Put `runs` (selecting each one's id and its owner's, `owner_id`) in their stores'
    outbox, if their owner has the export on; see `enqueue_runs`."""
    runs = runs.subquery()
    exporting: ColumnElement[bool] = User.export_run_history
    if deleting:
        exporting = or_(exporting, DataStore.history_revision.is_not(None))
    rows = (
        select(DataStore.id, literal(kind, HistoryExport.kind.type), runs.c.id)
        .join(User, User.id == DataStore.owner_user_id)
        .join(runs, runs.c.owner_id == User.id)
        .where(exporting)
        # Rows are locked in run order, as the export marks them, so two writers of several
        # don't each hold one the other wants.
        .order_by(runs.c.id)
    )
    stmt = insert(HistoryExport).from_select(
        [HistoryExport.data_store_id, HistoryExport.kind, HistoryExport.source_id], rows
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


def enqueue_runs(db: Session, *where: ColumnElement[bool], deleting: bool = False) -> None:
    """Put the agent runs matching `where` in their stores' outbox, if their owner has the
    export on; in `db`'s transaction, which the caller commits (the stores' exports are queued
    then). A run already there is pending again.

    For runs being deleted, call it before deleting them, with `deleting`: they're found
    through their rows. Their copies are kept while the export is off, so they go in the outbox
    then too, if the store has its `history` tables, and are deleted once it's back on. The
    workflow runs whose steps held them go in too: the steps stay, without them."""
    runs = (
        select(AgentRun.id, Agent.owner_id).join(Agent, Agent.id == AgentRun.agent_id).where(*where)
    )
    _enqueue(db, HistoryExportKind.agent_run, runs, deleting)
    if deleting:
        held = select(WorkflowRunStep.run_id).where(
            WorkflowRunStep.agent_run_id.in_(select(runs.subquery().c.id))
        )
        enqueue_workflow_runs(db, WorkflowRun.id.in_(held))


def enqueue_workflow_runs(db: Session, *where: ColumnElement[bool], deleting: bool = False) -> None:
    """Put the workflow runs matching `where` in their stores' outbox, as `enqueue_runs` does
    agent runs. Whatever changes a workflow run's status calls it, in the same transaction:
    ending it (`aios.workflows._end_runs`), deleting it, and pausing and resuming it, once runs
    can pause. A writer of both kinds writes its agent runs' rows first, as the export marks
    them."""
    runs = (
        select(WorkflowRun.id, Workflow.owner_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(*where)
    )
    _enqueue(db, HistoryExportKind.workflow_run, runs, deleting)


def enqueue_everything(db: Session, user_id: int) -> None:
    """Put every run of the user's, of each kind, in their store's outbox (a backfill)."""
    enqueue_runs(db, Agent.owner_id == user_id)
    enqueue_workflow_runs(db, Workflow.owner_id == user_id)


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
    """The store is ready, has its `history` tables, and its owner has the export on.

    Raises `DataStoreBehind` if its `history` tables are behind the line's head (their
    migration failed, say): they may not all be there yet."""
    row = db.execute(
        select(User.export_run_history, DataStore.history_revision)
        .join(DataStore, DataStore.owner_user_id == User.id)
        .where(
            DataStore.id == data_store_id,
            DataStore.status == DataStoreStatus.ready,
            DataStore.history_revision.is_not(None),
        )
    ).one_or_none()
    if row is None or not row.export_run_history:
        return False
    _check_head(data_store_id, row.history_revision)
    return True


class NoHistory(Exception):
    """The store has no `history` tables: its owner has never switched the export on."""


def check_searchable(data_store_id: uuid.UUID) -> None:
    """Raises unless the store's exported history can be searched (`aios.search`):
    `DataStoreNotReady` unless the store is ready, `NoHistory` if it has no `history` tables,
    and `DataStoreBehind` if they're behind their line's head."""
    with SessionLocal() as db:
        row = db.execute(
            select(DataStore.status, DataStore.history_revision).where(
                DataStore.id == data_store_id
            )
        ).one_or_none()
    if row is None:
        raise DataStoreNotFound(data_store_id)
    if row.status != DataStoreStatus.ready:
        raise DataStoreNotReady(data_store_id, row.status)
    if row.history_revision is None:
        raise NoHistory
    _check_head(data_store_id, row.history_revision)


def _check_head(data_store_id: uuid.UUID, revision: str) -> None:
    """Raises `DataStoreBehind` if a store's `history` line is at `revision`, before its head."""
    if revision in migrations.history_revisions_before_head():
        raise DataStoreBehind(data_store_id, revision)


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


def _upsert(store: Session, table: type, rows: list[dict[str, Any]]) -> None:
    """Insert `rows` into `table`, or update the rows already there with their ids."""
    if not rows:
        return
    stmt = insert(table).values(rows)
    store.execute(
        stmt.on_conflict_do_update(
            index_elements=[table.id], set_={c: stmt.excluded[c] for c in rows[0] if c != "id"}
        )
    )


def _write_run(store: Session, run: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    _upsert(store, HistoryAgentRun, [{**run, "exported_at": func.now()}])
    for start in range(0, len(messages), ROWS_PER_INSERT):
        stmt = insert(HistoryMessage).values(messages[start : start + ROWS_PER_INSERT])
        store.execute(
            stmt.on_conflict_do_update(
                index_elements=[HistoryMessage.agent_run_id, HistoryMessage.position],
                set_={
                    **{
                        c: stmt.excluded[c]
                        for c in messages[0]
                        if c not in ("agent_run_id", "position")
                    },
                    # Kept while its text is, so a run exported again is embedded only where
                    # it's new.
                    "embedding": case(
                        (HistoryMessage.text == stmt.excluded.text, HistoryMessage.embedding),
                        else_=None,
                    ),
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


def _load_workflow_runs(run_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The workflow runs that still exist, as `history.workflow_runs` rows, each with its
    steps (at every depth) as `history.workflow_run_steps` rows under "steps"."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                WorkflowRun.id,
                WorkflowRun.workflow_id,
                Workflow.name.label("workflow_name"),
                WorkflowVersion.version.label("workflow_version"),
                # Who ran it: today, a workflow's runs are its owner's.
                Workflow.owner_id.label("user_id"),
                cast(WorkflowRun.status, Text).label("status"),
                WorkflowRun.input,
                WorkflowRun.output,
                WorkflowRun.error,
                WorkflowRun.created_at,
                WorkflowRun.started_at,
                WorkflowRun.finished_at,
            )
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .where(WorkflowRun.id.in_(run_ids))
        ).mappings()
        runs = {r["id"]: {**_clean(dict(r)), "steps": []} for r in rows}
        steps = db.execute(
            select(
                WorkflowRunStep.id,
                WorkflowRunStep.run_id.label("workflow_run_id"),
                WorkflowRunStep.path,
                cast(WorkflowRunStep.kind, Text).label("kind"),
                cast(WorkflowRunStep.status, Text).label("status"),
                WorkflowRunStep.input,
                WorkflowRunStep.output,
                WorkflowRunStep.error,
                WorkflowRunStep.agent_run_id,
                WorkflowRunStep.started_at,
                WorkflowRunStep.finished_at,
            )
            .where(WorkflowRunStep.run_id.in_(list(runs)))
            .order_by(WorkflowRunStep.id)
        ).mappings()
        for step in steps:
            runs[step["workflow_run_id"]]["steps"].append(_clean(dict(step)))
        return runs


def _write_workflow_run(store: Session, run: dict[str, Any]) -> None:
    steps = run["steps"]
    row = {k: v for k, v in run.items() if k != "steps"}
    _upsert(store, HistoryWorkflowRun, [{**row, "exported_at": func.now()}])
    for start in range(0, len(steps), ROWS_PER_INSERT):
        _upsert(store, HistoryWorkflowRunStep, steps[start : start + ROWS_PER_INSERT])


@dataclass
class _Batch:
    """A batch of a store's pending outbox rows, as it's exported: what still exists of each
    kind, read from the platform, and the rows that failed."""

    pending: list[Row]
    # Of each kind, the runs that still exist, as their copies' rows, by id.
    found: dict[HistoryExportKind, dict[int, Any]] = field(default_factory=dict)
    # Each agent run's messages, as `history.messages` rows.
    messages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # Outbox row ids that failed, with their errors.
    errors: dict[int, str] = field(default_factory=dict)

    def of(self, kind: HistoryExportKind) -> list[Row]:
        return [p for p in self.pending if p.kind == kind]

    def exists(self, p: Row) -> bool:
        return p.source_id in self.found[p.kind]

    def failed(self, p: Row, exc: Exception, data_store_id: uuid.UUID) -> None:
        log.exception("Couldn't export %s %s to data store %s", p.kind, p.source_id, data_store_id)
        self.errors[p.id] = f"{type(exc).__name__}: {exc}"


# How each kind's copy is deleted, once its run has gone (its child rows go with it, by their
# foreign keys).
_COPIES = {
    HistoryExportKind.agent_run: HistoryAgentRun,
    HistoryExportKind.workflow_run: HistoryWorkflowRun,
}


def _read(batch: _Batch, data_store_id: uuid.UUID) -> None:
    """Read what the batch's runs are now, from the platform."""
    for kind, load in (
        (HistoryExportKind.agent_run, _load_runs),
        (HistoryExportKind.workflow_run, _load_workflow_runs),
    ):
        ids = [p.source_id for p in batch.of(kind)]
        batch.found[kind] = load(ids) if ids else {}
    agent_runs = [p for p in batch.of(HistoryExportKind.agent_run) if batch.exists(p)]
    if not agent_runs:
        return
    # Every thread read first, on one connection, so the store's transaction isn't left idle.
    with checkpoints.sync_saver() as saver:
        for p in agent_runs:
            try:
                batch.messages[p.source_id] = flatten(
                    p.source_id, checkpoints.history(p.source_id, saver)
                )
            except Exception as exc:
                batch.failed(p, exc, data_store_id)


def _write(store: Session, batch: _Batch, p: Row) -> None:
    run = batch.found[p.kind][p.source_id]
    if p.kind == HistoryExportKind.agent_run:
        _write_run(store, run, batch.messages[p.source_id])
    else:
        _write_workflow_run(store, run)


def _export_batch(data_store_id: uuid.UUID) -> int:
    """Export the store's oldest pending runs, of any kind; how many there were."""
    with SessionLocal() as db:
        pending = db.execute(
            select(
                HistoryExport.id,
                HistoryExport.kind,
                HistoryExport.source_id,
                HistoryExport.enqueued_at,
            )
            .where(
                HistoryExport.data_store_id == data_store_id,
                _pending(),
            )
            .order_by(HistoryExport.enqueued_at)
            .limit(BATCH)
        ).all()
    if not pending:
        return 0
    batch = _Batch(pending)
    # A run that fails (its checkpoint won't load, its copy won't write) is set aside with its
    # error, until it changes again or RETRY_AFTER, so the runs after it still go in.
    _read(batch, data_store_id)
    with store_session(data_store_id, "rw") as store:
        for p in pending:
            if not batch.exists(p):
                copy = _COPIES[p.kind]
                store.execute(delete(copy).where(copy.id == p.source_id))
            elif p.id not in batch.errors:
                try:
                    with store.begin_nested():
                        _write(store, batch, p)
                except Exception as exc:
                    batch.failed(p, exc, data_store_id)
        store.commit()
    with SessionLocal() as db:
        # In kind then run order, as writers enqueue them (`enqueue_workflow_runs`).
        for p in sorted(pending, key=lambda p: (p.kind, p.source_id)):
            # Only as it was read: a run changed since is pending again, for the next batch.
            same = (HistoryExport.id == p.id, HistoryExport.enqueued_at == p.enqueued_at)
            if p.id in batch.errors:
                db.execute(
                    update(HistoryExport)
                    .where(*same)
                    .values(
                        error=batch.errors[p.id],
                        retry_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, RETRY_AFTER),
                    )
                )
            elif batch.exists(p):
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
        _caught_up(data_store_id, history_exported_at=func.now())
        _embed(data_store_id)
    return exported


def _caught_up(data_store_id: uuid.UUID, **values: Any) -> None:
    with SessionLocal() as db:
        db.execute(update(DataStore).where(DataStore.id == data_store_id).values(**values))
        db.commit()


def _embedding(data_store_id: uuid.UUID) -> bool:
    """The store's owner has embedding on (which they can only while the export is on)."""
    with SessionLocal() as db:
        return bool(
            db.scalar(
                select(User.embed_run_history)
                .join(DataStore, DataStore.owner_user_id == User.id)
                .where(DataStore.id == data_store_id)
            )
        )


def _embed_batch(data_store_id: uuid.UUID) -> int:
    """Embed the store's first messages waiting for it; how many there were."""
    messages = HistoryMessage.__table__
    with store_session(data_store_id, "rw") as store:
        waiting = store.execute(
            select(messages.c.agent_run_id, messages.c.position, messages.c.text)
            .where(messages.c.embedding.is_(None), sql_text(EMBEDDABLE))
            .order_by(messages.c.agent_run_id, messages.c.position)
            .limit(EMBED_BATCH)
        ).all()
        # Not left idle in a transaction while the embedding API answers.
        store.rollback()
        if not waiting:
            return 0
        vectors = asyncio.run(embed_documents([m.text for m in waiting]))
        store.execute(
            messages.update()
            .where(
                messages.c.agent_run_id == bindparam("run"),
                messages.c.position == bindparam("pos"),
            )
            .values(embedding=bindparam("vec", type_=messages.c.embedding.type)),
            [
                {"run": m.agent_run_id, "pos": m.position, "vec": vec}
                for m, vec in zip(waiting, vectors, strict=True)
            ],
        )
        store.commit()
    return len(waiting)


def _embed(data_store_id: uuid.UUID) -> None:
    """Embed the store's exported messages still waiting for it (`EMBEDDABLE`), while its owner
    has embedding on: those just exported, or every one there once it's switched on (a
    backfill). For up to `EMBED_BUDGET`; it has caught up (`history_embedded_at`) once none are
    left. What it doesn't finish (out of time, or the embedding API failed) waits for the
    periodic sweep. Switched off, it stops after the batch under way, keeping what's embedded.

    Raises `DataStoreBusy` if the store has no connections free."""
    deadline = time.monotonic() + EMBED_BUDGET
    try:
        while _embedding(data_store_id):
            if _embed_batch(data_store_id) < EMBED_BATCH:
                _caught_up(data_store_id, history_embedded_at=func.now())
                return
            if time.monotonic() >= deadline:
                break
        else:
            return
    except DataStoreBusy:
        raise
    except Exception:
        log.exception("Couldn't embed the exported history of data store %s", data_store_id)
    # Not caught up: the sweep queues the store's export again (`stores_to_export`).
    _caught_up(data_store_id, history_embedded_at=None)


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
                        # Messages still to embed (a backfill unfinished, or that failed).
                        and_(User.embed_run_history, DataStore.history_embedded_at.is_(None)),
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
        _check_head(data_store_id, store.history_revision)
        with store_session(data_store_id, "rw") as store:
            # Children first, each in one statement: what their foreign keys would cascade
            # row by row (millions of messages, say) is left with nothing to do.
            store.execute(delete(HistoryMessage))
            store.execute(delete(HistoryAgentRun))
            store.execute(delete(HistoryWorkflowRunStep))
            store.execute(delete(HistoryWorkflowRun))
            store.commit()
    # Once the lock is let go: the export this queues may start at once, and would find it held.
    with SessionLocal() as db:
        db.execute(
            update(DataStore).where(DataStore.id == data_store_id).values(history_exported_at=None)
        )
        enqueue_everything(db, owner_id)
        db.commit()


def set_exporting(db: Session, user_id: int, on: bool) -> uuid.UUID | None:
    """Switch the user's export on or off, in `db`'s transaction, which the caller commits.

    Switching it on (from off) backfills: every run of theirs goes in the outbox, and the store
    hasn't caught up until they're exported. It returns their store, to queue its set-up once
    committed (`enqueue_set_up`)."""
    # Locked, so two switches at once don't both backfill, or both miss it.
    was_on = db.scalar(select(User.export_run_history).where(User.id == user_id).with_for_update())
    if not on:
        # Embedding is only offered while the export is on.
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(export_run_history=False, embed_run_history=False)
        )
        return None
    db.execute(update(User).where(User.id == user_id).values(export_run_history=True))
    if was_on:
        return None
    from aios.datastores.resolve import data_store_for

    store_id = data_store_for(user_id, db)
    enqueue_everything(db, user_id)
    db.execute(update(DataStore).where(DataStore.id == store_id).values(history_exported_at=None))
    return store_id


class ExportOff(Exception):
    """Embedding was asked for while the user's run history export is off."""


def set_embedding(db: Session, user_id: int, on: bool) -> None:
    """Switch embedding the user's exported messages on or off, in `db`'s transaction, which
    the caller commits. Raises `ExportOff` to switch it on while the export is off.

    Switching it on (from off) backfills: the store's export is queued once committed, and
    embeds every exported message waiting (`_embed`); it hasn't caught up until then."""
    row = db.execute(
        select(User.export_run_history, User.embed_run_history)
        .where(User.id == user_id)
        .with_for_update()
    ).one()
    if on and not row.export_run_history:
        raise ExportOff
    db.execute(update(User).where(User.id == user_id).values(embed_run_history=on))
    if not on or row.embed_run_history:
        return
    from aios.datastores.resolve import data_store_for

    store_id = data_store_for(user_id, db)
    db.execute(update(DataStore).where(DataStore.id == store_id).values(history_embedded_at=None))
    db.info.setdefault(_TO_EXPORT, set()).add(store_id)
