"""The sync loop every source kind shares: one sync of a source (`run_sync`), run by the run
service's `sources` lane (`aios.run_service.work.source_sync`) under each organisation's fair
share.

**A sync** reads what changed at the source since its cursor (an incremental or manual sync), or
everything in scope (a full one: reconciliation, daily or on demand, and a source's first sync),
through its kind's adapter (`aios.source_providers`). Then:

- it writes items by external id, idempotently, at most `sync_batch_items` per transaction;
- an item whose content hash is unchanged isn't fetched or indexed again;
- a message item's text (cut at `sync_message_text_cap_bytes`) is checked by Jev
  (`aios.verdicts.check_synced_item`) and indexed straight into search as `source_item` pieces;
- a file item becomes a document, stored in S3 as uploads are and ingested by the existing
  pipeline (`aios.ingest`), which checks it with Jev; the document is linked to its item;
- what the change feed says was removed, and (in a full sync) what the listing no longer has,
  is removed with its document, chunks and search entries;
- items still awaiting Jev's check are checked again;
- the cursor moves on only when the sync commits: a sync that fails part way leaves it where it
  was, and the next sync reads the same changes again, writing nothing twice and re-indexing
  nothing unchanged.

**One at a time.** A sync is recorded running (`SourceSync`) before it starts, and a partial
unique index keeps one running per source: a second is skipped. One running longer than
`sync_stale_seconds` is taken to have died.

**At the storage cap** (`aios.datastores.quota.room_for`) a sync skips new items, counting them,
records "storage full", and still applies updates and removals.

**Failing.** A failed sync counts against its source; after `sync_failures_before_failing` in a
row the source is `failing` and is retried only every `sync_failing_retry_seconds`. A final
authorisation failure (`NeedsReconnecting`) makes it `needs_reauth`. Either change tells every
admin, once (`aios.notifications.SOURCE_ATTENTION`). A sync that succeeds makes it active again.

**Health** (`health`) is derived from its status and recent syncs: healthy, delayed, failing or
needs reconnecting.

**Kinds.** A URL source's adapter fetches its page (`aios.url_sources`); a connector's and the
test provider's are their provider's (`aios.source_providers`). Webhook and email sources are
pushed to (`PUSHED`): they never sync, so their items are never reconciled away. What's pushed
to them is written by the same code (`push`), recorded as a sync of its own.
"""

import contextlib
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from aios import notifications, search_index, verdicts
from aios.config import get_settings
from aios.datastores import files, quota, store_session
from aios.datastores.models import (
    Document,
    DocumentStatus,
    Source,
    SourceKind,
    SourceStatus,
    SourceSync,
    SyncedItem,
    SyncedItemKind,
    SyncKind,
    SyncStatus,
)
from aios.jev import Verdict
from aios.source_providers import (
    Adapter,
    Content,
    Item,
    NeedsReconnecting,
    SourceView,
    adapter_for,
    why_unavailable,
)

log = logging.getLogger(__name__)

# What a sync record says when its sync died part way (its process stopped).
STOPPED = "The sync stopped part way; the next sync carries on from where it last committed."
STORAGE_FULL = "Storage full: new items were skipped until there's room."

# Where a source's settings keep the parts of its scope needing an admin's attention, by scope
# option id, each with why (`Changes.attention`), as its last sync found them.
ATTENTION = "attention"

HEALTHY = "healthy"
DELAYED = "delayed"
FAILING = "failing"
NEEDS_RECONNECTING = "needs_reconnecting"

# Where documents' ids come from: an item's file is stored under its document's id.
DOCUMENT_IDS = func.pg_get_serial_sequence(Document.__table__.fullname, "id")


# The kinds that are pushed to rather than synced: they never sync, and nothing reconciles them.
PUSHED = (SourceKind.webhook, SourceKind.email)


def view(source: Source, credential: str | None = None, resync: bool = False) -> SourceView:
    return SourceView(
        source.id, source.name, dict(source.settings or {}), credential, resync, source.cursor
    )


def _provider_on(source: Source | None) -> bool:
    """Whether the source's provider may sync here (a connector's provider can be switched
    off for the whole deployment at once). Other kinds have no provider to switch off."""
    if source is None or source.kind != SourceKind.connector:
        return True
    return why_unavailable((source.settings or {}).get("provider")) is None


def interval(source: Source) -> int:
    return source.interval_seconds or get_settings().sync_interval_seconds


# --- One at a time -------------------------------------------------------------------------------


def _expire_stale(store: Session, source_id: int) -> None:
    """Fail the source's syncs still marked running past `sync_stale_seconds`: they died."""
    stale = func.now() - timedelta(seconds=get_settings().sync_stale_seconds)
    store.execute(
        update(SourceSync)
        .where(
            SourceSync.source_id == source_id,
            SourceSync.status == SyncStatus.running,
            SourceSync.started_at < stale,
        )
        .values(status=SyncStatus.failed, finished_at=func.now(), error=STOPPED)
    )


def running(store: Session, source_id: int) -> bool:
    """Whether a sync of the source is running now."""
    stale = func.now() - timedelta(seconds=get_settings().sync_stale_seconds)
    return (
        store.scalar(
            select(SourceSync.id).where(
                SourceSync.source_id == source_id,
                SourceSync.status == SyncStatus.running,
                SourceSync.started_at >= stale,
            )
        )
        is not None
    )


def _start(store: Session, source_id: int, kind: SyncKind) -> SourceSync | None:
    """Record a sync of the source running (committed); None, with nothing recorded, if one is
    running already or the source doesn't sync now (Uploads, paused, being removed, gone)."""
    source = store.scalar(
        select(Source)
        .where(Source.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        source is None
        or source.kind == SourceKind.upload
        or source.kind in PUSHED
        or source.status in (SourceStatus.paused, SourceStatus.removing)
    ):
        store.rollback()
        return None
    _expire_stale(store, source_id)
    if running(store, source_id):
        store.rollback()
        return None
    sync = SourceSync(source_id=source_id, kind=kind)
    store.add(sync)
    store.commit()
    return sync


# --- Writing items -------------------------------------------------------------------------------


@dataclass
class Counts:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    dropped: int = 0
    storage_full: bool = False


def _cap(text: str) -> tuple[str, bool]:
    """A message's text cut at `sync_message_text_cap_bytes` (on a character boundary), and
    whether it was."""
    cap = get_settings().sync_message_text_cap_bytes
    encoded = text.encode()
    if len(encoded) <= cap:
        return text, False
    return encoded[:cap].decode(errors="ignore"), True


@dataclass
class _Batch:
    """What a batch did that's acted on once it commits, or undone if it doesn't."""

    # Documents to ingest.
    ingest: list[int]
    # Files stored for new documents, deleted again should the batch roll back.
    new_keys: list[str]
    # Released items that changed, whose admins are told.
    unreleased: list[int]


class _Sync:
    def __init__(
        self,
        store: Session,
        store_id: uuid.UUID,
        source: Source,
        adapter: Adapter,
        credential: str | None = None,
        resync: bool = False,
    ):
        self.store = store
        self.store_id = store_id
        self.source_id = source.id
        self.label = f"source:{source.name}"
        self.view = view(source, credential, resync)
        self.adapter = adapter
        self.counts = Counts()

    def write_all(self, items: list[Item]) -> None:
        size = get_settings().sync_batch_items
        for start in range(0, len(items), size):
            batch = _Batch([], [], [])
            try:
                for item in items[start : start + size]:
                    self._write(item, batch)
                self.store.commit()
            except BaseException:
                self.store.rollback()
                for key in batch.new_keys:
                    with contextlib.suppress(Exception):
                        files.delete(key)
                raise
            self._after(batch)

    def _after(self, batch: _Batch) -> None:
        for item_id in batch.unreleased:
            notifications.notify_admins_from_store(
                self.store_id, notifications.RELEASED_ITEM_CHANGED, {"synced_item_id": item_id}
            )
        if batch.ingest:
            _ingest(self.store_id, batch.ingest)

    def _existing(self, external_id: str) -> SyncedItem | None:
        return self.store.scalar(
            select(SyncedItem).where(
                SyncedItem.source_id == self.source_id, SyncedItem.external_id == external_id
            )
        )

    def _parent(self, item: Item) -> int | None:
        if item.parent_external_id is None:
            return None
        parent = self._existing(item.parent_external_id)
        return parent.id if parent else None

    def _write(self, item: Item, batch: _Batch) -> None:
        existing = self._existing(item.external_id)
        if existing is not None and existing.kind != item.kind:
            # A message become a file, or the other way: written afresh.
            self._remove(existing)
            existing = None
        if existing is not None and existing.content_hash == item.content_hash:
            existing.last_seen_at = func.now()
            existing.title, existing.url = item.title, item.url
            return
        if existing is None and not quota.room_for(self.store):
            self.counts.skipped += 1
            self.counts.storage_full = True
            return
        content = self.adapter.fetch(self.view, item)
        if item.kind == SyncedItemKind.file:
            self._write_file(item, existing, content.data or b"", batch)
        else:
            self._write_message(item, existing, content.text or "", batch)

    def _fields(self, row: SyncedItem, item: Item) -> None:
        row.title = item.title
        row.url = item.url
        row.author_name = item.author_name
        row.source_updated_at = item.updated_at
        row.content_hash = item.content_hash
        row.parent_id = self._parent(item)
        row.last_seen_at = func.now()

    def _write_message(
        self, item: Item, existing: SyncedItem | None, text: str, batch: _Batch
    ) -> None:
        row = existing or SyncedItem(
            source_id=self.source_id, external_id=item.external_id, kind=SyncedItemKind.message
        )
        self._fields(row, item)
        row.text, row.truncated = _cap(text)
        if existing is None:
            self.store.add(row)
            self.counts.added += 1
        else:
            self.counts.updated += 1
        self.store.flush()
        if existing is not None and existing.verdict == Verdict.released:
            verdicts.unrelease_item(self.store, row.id)
            batch.unreleased.append(row.id)
        else:
            row.verdict = Verdict.awaiting_check
        self.store.flush()
        # Indexed unsearchable until it's checked, then given its verdict.
        search_index.index_synced_item(self.store, row)
        verdicts.check_synced_item(self.store, row.id)
        self.store.expire(row)

    def _write_file(
        self, item: Item, existing: SyncedItem | None, data: bytes, batch: _Batch
    ) -> None:
        if existing is None and not quota.room_for(self.store, len(data)):
            self.counts.skipped += 1
            self.counts.storage_full = True
            return
        row = existing or SyncedItem(
            source_id=self.source_id, external_id=item.external_id, kind=SyncedItemKind.file
        )
        self._fields(row, item)
        if existing is None:
            self.store.add(row)
            self.store.flush()
            doc_id = self.store.scalar(select(func.nextval(DOCUMENT_IDS)))
            key = files.document_key(self.store_id, doc_id)
            batch.new_keys.append(key)
            stored = files.put(key, io.BytesIO(data), item.content_type)
            self.store.add(
                Document(
                    id=doc_id,
                    filename=item.title,
                    content_type=item.content_type,
                    s3_key=key,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    source_id=self.source_id,
                    synced_item_id=row.id,
                )
            )
            self.counts.added += 1
        else:
            doc = self.store.scalar(select(Document).where(Document.synced_item_id == row.id))
            if doc is None:
                self.counts.dropped += 1
                return
            stored = files.put(doc.s3_key, io.BytesIO(data), item.content_type)
            doc_id = doc.id
            doc.filename, doc.content_type = item.title, item.content_type
            doc.size_bytes, doc.sha256 = stored.size_bytes, stored.sha256
            doc.status, doc.error, doc.attempts = DocumentStatus.pending, None, 0
            self.counts.updated += 1
        if row.verdict != Verdict.released:
            row.verdict = Verdict.awaiting_check
        self.store.flush()
        batch.ingest.append(doc_id)

    # --- Removing ---

    def _remove(self, row: SyncedItem) -> None:
        _remove_item(self.store, row.id)

    def remove_all(self, external_ids: set[str]) -> None:
        ids = sorted(external_ids)
        size = get_settings().sync_batch_items
        for start in range(0, len(ids), size):
            rows = self.store.scalars(
                select(SyncedItem).where(
                    SyncedItem.source_id == self.source_id,
                    SyncedItem.external_id.in_(ids[start : start + size]),
                )
            ).all()
            for row in rows:
                self._remove(row)
                self.counts.removed += 1
            self.store.commit()

    def missing_from(self, listed: list[Item], covers=None) -> set[str]:
        """The source's items a read covering them (all of them, unless `covers` says which)
        no longer has."""
        have = set(
            self.store.scalars(
                select(SyncedItem.external_id).where(SyncedItem.source_id == self.source_id)
            )
        )
        if covers is not None:
            have = {e for e in have if covers(e)}
        return have - {i.external_id for i in listed}

    # --- Checking again ---

    def recheck(self) -> None:
        """Check the source's items still awaiting Jev again, whether or not they changed: its
        message items here, its file items' documents as ingest would. Stops at the first Jev
        can't check (it's down)."""
        waiting = self.store.execute(
            select(SyncedItem.id, SyncedItem.kind).where(
                SyncedItem.source_id == self.source_id,
                SyncedItem.verdict == Verdict.awaiting_check,
            )
        ).all()
        for item_id, kind in waiting:
            if kind == SyncedItemKind.message:
                verdict = verdicts.check_synced_item(self.store, item_id)
                self.store.commit()
            else:
                doc_id = self.store.scalar(
                    select(Document.id).where(
                        Document.synced_item_id == item_id,
                        Document.status == DocumentStatus.ready,
                    )
                )
                if doc_id is None:
                    continue  # still being ingested, which checks it
                verdict = verdicts.check_document(self.store, doc_id)
            if verdict == Verdict.awaiting_check:
                return

    def awaiting(self) -> int:
        return self.store.scalar(
            select(func.count()).where(
                SyncedItem.source_id == self.source_id,
                SyncedItem.verdict == Verdict.awaiting_check,
            )
        )


def _remove_item(store: Session, item_id: int) -> None:
    """Remove an item, with its document (its file first), chunks and search entries."""
    doc = store.execute(
        select(Document.id, Document.s3_key).where(Document.synced_item_id == item_id)
    ).one_or_none()
    if doc is not None:
        files.delete(doc.s3_key)
        store.execute(delete(Document).where(Document.id == doc.id))
    store.execute(delete(SyncedItem).where(SyncedItem.id == item_id))


def remove_items(store: Session, source_id: int) -> None:
    """Remove every item of the source, with their documents, chunks and search entries,
    `sync_batch_items` per transaction."""
    size = get_settings().sync_batch_items
    while ids := store.scalars(
        select(SyncedItem.id).where(SyncedItem.source_id == source_id).limit(size)
    ).all():
        for item_id in ids:
            _remove_item(store, item_id)
        store.commit()


def queue_ingest(store_id: uuid.UUID, document_ids: list[int]) -> None:
    _ingest(store_id, document_ids)


def _ingest(store_id: uuid.UUID, document_ids: list[int]) -> None:
    """Queue the documents for ingestion. One that can't be queued is left pending: the next
    change to its item queues it again."""
    from aios.worker import ingest_document_task

    for doc_id in document_ids:
        try:
            ingest_document_task.delay(str(store_id), doc_id)
        except Exception:
            log.exception("couldn't queue synced document %s of store %s", doc_id, store_id)


# --- One sync ----------------------------------------------------------------------------------


def _adapter(source: Source) -> Adapter:
    if source.kind == SourceKind.url:
        from aios.url_sources import UrlAdapter

        return UrlAdapter()
    return adapter_for(source.settings or {})


def _full(source: Source, kind: SyncKind) -> bool:
    """Whether this sync lists everything: a full one, the source's first, or reconciliation
    being due."""
    if kind == SyncKind.full or source.cursor is None or source.last_reconciled_at is None:
        return True
    due = datetime.now(UTC) - timedelta(seconds=get_settings().sync_reconcile_interval_seconds)
    return source.last_reconciled_at <= due


def run_sync(
    data_store_id: uuid.UUID, source_id: int, kind: SyncKind = SyncKind.incremental
) -> SourceSync | None:
    """Run one sync of the source (see the module), and return its record; None if it was
    skipped (another is running, or the source doesn't sync now). Syncs aren't reachable over
    HTTP: the run service runs this, and so do tests."""
    with store_session(data_store_id, "rw") as store:
        if not _provider_on(store.get(Source, source_id)):
            # Switched off in this deployment: it doesn't sync, and its content stays.
            return None
        sync = _start(store, source_id, kind)
        if sync is None:
            return None
        sync_id = sync.id
        source = store.get_one(Source, source_id)
        full = _full(source, kind)
        if full and kind != SyncKind.full:
            store.execute(
                update(SourceSync).where(SourceSync.id == sync_id).values(kind=SyncKind.full)
            )
            store.commit()
        run = None
        try:
            adapter = _adapter(source)
            credential = None
            if source.connector_id is not None:
                from aios import connectors

                credential = connectors.credential(data_store_id, source.connector_id)
            run = _Sync(
                store, data_store_id, source, adapter, credential, resync=kind == SyncKind.full
            )
            adapter = run.adapter
            changes = (
                adapter.listing(run.view) if full else adapter.changes(run.view, source.cursor)
            )
            run.write_all(changes.changed)
            removed = set(changes.removed)
            if changes.covers is not None or full:
                removed |= run.missing_from(changes.changed, changes.covers)
            run.remove_all(removed)
            run.recheck()
        except Exception as exc:
            counts = run.counts if run is not None else Counts()
            _failed(store, data_store_id, source_id, sync_id, counts, exc)
        else:
            _succeeded(
                store,
                source_id,
                sync_id,
                run.counts,
                changes.cursor,
                full,
                run.awaiting(),
                changes.attention,
            )
            with contextlib.suppress(Exception):
                quota.record_files(data_store_id, store)
        prune(store, source_id)
        store.commit()
        record = store.get_one(SourceSync, sync_id, populate_existing=True)
        store.expunge(record)
        return record


def _counted(sync_id: int, counts: Counts, **values):
    return (
        update(SourceSync)
        .where(SourceSync.id == sync_id)
        .values(
            finished_at=func.now(),
            added=counts.added,
            updated=counts.updated,
            removed=counts.removed,
            skipped=counts.skipped,
            dropped=counts.dropped,
            storage_full=counts.storage_full,
            **values,
        )
    )


def _succeeded(
    store: Session,
    source_id: int,
    sync_id: int,
    counts: Counts,
    cursor: str | None,
    full: bool,
    awaiting: int,
    attention: dict[str, str] | None = None,
) -> None:
    source = store.scalar(select(Source).where(Source.id == source_id).with_for_update())
    source.cursor = cursor
    if attention is not None:
        settings = {k: v for k, v in (source.settings or {}).items() if k != ATTENTION}
        source.settings = {**settings, ATTENTION: attention} if attention else settings
    if full:
        source.last_reconciled_at = func.now()
    source.failure_count = 0
    if source.status in (SourceStatus.failing, SourceStatus.needs_reauth):
        source.status = SourceStatus.active
    source.next_sync_at = func.now() + timedelta(seconds=interval(source))
    store.execute(
        _counted(
            sync_id,
            counts,
            status=SyncStatus.succeeded,
            awaiting_check=awaiting,
            error=STORAGE_FULL if counts.storage_full else None,
        )
    )
    store.commit()


def _message(exc: Exception) -> str:
    said = str(exc).strip()
    return (said or type(exc).__name__)[:2000]


def _failed(
    store: Session,
    store_id: uuid.UUID,
    source_id: int,
    sync_id: int,
    counts: Counts,
    exc: Exception,
) -> None:
    """Record the failed sync, count it against its source, and tell the admins should that
    make the source failing or needing reconnecting. The cursor stays where it was."""
    store.rollback()
    log.warning("sync %s of source %s failed: %s", sync_id, source_id, exc)
    settings = get_settings()
    source = store.scalar(select(Source).where(Source.id == source_id).with_for_update())
    before = source.status
    source.failure_count += 1
    if isinstance(exc, NeedsReconnecting):
        source.status = SourceStatus.needs_reauth
        if source.connector_id is not None:
            from aios import connectors

            connectors.needs_reconnecting(store, store_id, source.connector_id)
    elif (
        source.failure_count >= settings.sync_failures_before_failing
        and source.status == SourceStatus.active
    ):
        source.status = SourceStatus.failing
    retry = (
        settings.sync_failing_retry_seconds
        if source.status == SourceStatus.failing
        else interval(source)
    )
    source.next_sync_at = func.now() + timedelta(seconds=retry)
    awaiting = store.scalar(
        select(func.count()).where(
            SyncedItem.source_id == source_id, SyncedItem.verdict == Verdict.awaiting_check
        )
    )
    store.execute(
        _counted(
            sync_id, counts, status=SyncStatus.failed, awaiting_check=awaiting, error=_message(exc)
        )
    )
    became = source.status if source.status != before else None
    store.commit()
    if became in (SourceStatus.failing, SourceStatus.needs_reauth):
        notifications.notify_admins_from_store(
            store_id, notifications.SOURCE_ATTENTION, {"source_id": source_id}
        )


# --- Pushed items ----------------------------------------------------------------------------


class _Given:
    """The adapter for content already in hand: what was pushed to a source."""

    def __init__(self, contents: dict[str, Content]):
        self.contents = contents

    def fetch(self, source: SourceView, item: Item) -> Content:
        return self.contents[item.external_id]


def push(
    data_store_id: uuid.UUID,
    source_id: int,
    items: list[Item],
    contents: dict[str, Content],
    *,
    dropped: int = 0,
    error: str | None = None,
) -> SourceSync | None:
    """Write items pushed to a source (an email, and its attachments) as a sync writes them, Jev
    checking each, and record what was written, and any `dropped`, as a sync of its own. None
    if the source is gone or doesn't take them now (paused, being removed)."""
    with store_session(data_store_id, "rw") as store:
        source = store.get(Source, source_id)
        if source is None or source.status in (SourceStatus.paused, SourceStatus.removing):
            return None
        run = _Sync(store, data_store_id, source, _Given(contents))
        run.counts.dropped = dropped
        if items:
            run.write_all(items)
        return record_push(store, source_id, run.counts, run.awaiting(), error)


def record_push(
    store: Session, source_id: int, counts: Counts, awaiting: int, error: str | None = None
) -> SourceSync:
    """Record what was pushed to a source as a finished sync (committed)."""
    record = SourceSync(
        source_id=source_id,
        kind=SyncKind.incremental,
        status=SyncStatus.succeeded,
        finished_at=func.now(),
        added=counts.added,
        updated=counts.updated,
        dropped=counts.dropped,
        skipped=counts.skipped,
        storage_full=counts.storage_full,
        awaiting_check=awaiting,
        error=error or (STORAGE_FULL if counts.storage_full else None),
    )
    store.add(record)
    prune(store, source_id)
    store.commit()
    store.refresh(record)
    store.expunge(record)
    return record


def prune(store: Session, source_id: int | None = None) -> int:
    """Delete sync records older than `sync_history_days` (the source's, or every source's), in
    the caller's transaction; how many went. A running one is never pruned."""
    cutoff = func.now() - timedelta(days=get_settings().sync_history_days)
    statement = delete(SourceSync).where(
        SourceSync.started_at < cutoff, SourceSync.status != SyncStatus.running
    )
    if source_id is not None:
        statement = statement.where(SourceSync.source_id == source_id)
    return store.execute(statement).rowcount


# --- Health ------------------------------------------------------------------------------------


def health(source: Source, last: SourceSync | None, last_success: datetime | None) -> str:
    """The source's health, from its status and recent syncs: needs reconnecting or failing by
    its status; delayed if its last sync failed, or nothing has succeeded for two of its
    intervals; else healthy (as Uploads, and a paused source, always are)."""
    if source.kind in (SourceKind.upload, *PUSHED) or source.status == SourceStatus.paused:
        return HEALTHY
    if source.status == SourceStatus.needs_reauth:
        return NEEDS_RECONNECTING
    if source.status == SourceStatus.failing:
        return FAILING
    if last is not None and last.status == SyncStatus.failed:
        return DELAYED
    since = last_success or source.created_at
    if datetime.now(UTC) - since > timedelta(seconds=2 * interval(source)):
        return DELAYED
    return HEALTHY


# --- Due syncs -----------------------------------------------------------------------------------


def due(store: Session) -> list[int]:
    """The store's sources due to sync: active or failing, past their next sync, and with their
    provider on."""
    return [
        source.id
        for source in store.scalars(
            select(Source)
            .where(
                Source.kind.not_in([SourceKind.upload, *PUSHED]),
                Source.status.in_([SourceStatus.active, SourceStatus.failing]),
                (Source.next_sync_at.is_(None)) | (Source.next_sync_at <= func.now()),
            )
            .order_by(Source.id)
        )
        if _provider_on(source)
    ]
