"""Jev's verdicts on what a store keeps (`aios.jev.Verdict`): documents, checked when they're
ingested, their verdict stored on them and copied onto their search items.

- **Checking a document** (`check_document`): its chunks go to Jev (`aios.jev.check_all`); any
  flagged flags it, with Jev's reason. One Jev couldn't check stays `awaiting_check`, kept out of
  search and context: ingestion retries it (`aios.worker.check_document_task`), and the sweep
  checks anything still waiting.
- **The sweep** (`sweep`, every `jev_sweep_interval_seconds` per ready store): documents
  awaiting a check, and those Jev judged under an older version (its model or our policy,
  `aios.jev.policy`), are checked again. A released document is left released while its content
  is what the admin released; once it changes, it goes back to `awaiting_check` (its release
  undone) and the admin who released it is notified.
- **Releasing** (`release`): an admin releases a flagged document, which is then found by search
  and reaches context, still wrapped as untrusted. Recorded in the audit log.

**Synced items** (`aios.sync`): a message item is checked by its source's sync as it's written
and whenever its content changes (`check_synced_item`); one Jev couldn't check is checked again
by the next sync, or the sweep. A file item is its document, checked as every document is, and
its verdict is copied onto the item. A synced document's document ingested event waits for its
document to be found clean (`check_document`). Flagged items are on the flagged list
(`flagged_items`), and admins release them as they do documents (`release_item`).

Derived text (run messages, briefs, people's chat messages until they're checked at posting) is
indexed as `derived` by its writers (`aios.search_index`): never checked again.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aios import audit, jev, notifications, outbox
from aios.datastores import store_id_of, store_session
from aios.datastores.models import (
    Chunk,
    Document,
    DocumentStatus,
    SearchItem,
    SearchItemKind,
    Source,
    SyncedItem,
    SyncedItemKind,
)
from aios.jev import JevUnavailable, Verdict
from aios.models import EventType
from aios.notifications import notify_from_store

log = logging.getLogger(__name__)

# The notification the admin who released a document gets when its content changes.
RELEASED_DOCUMENT_CHANGED = notifications.RELEASED_DOCUMENT_CHANGED

# A document's chunks are classified this many at once; each batch within Jev's timeout.
DOCUMENT_BATCH = 16


def _texts(store: Session, document_id: int) -> list[str]:
    return list(
        store.scalars(
            select(Chunk.text).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
        )
    )


def content_hash(texts: list[str]) -> str:
    """The SHA-256 (hex) of a document's content: its chunks' text, in order."""
    digest = hashlib.sha256()
    for t in texts:
        digest.update(hashlib.sha256(t.encode()).digest())
    return digest.hexdigest()


def _set(store: Session, document_id: int, verdict: Verdict, **values) -> None:
    """The document's verdict, and its search items' (and, for a synced document, its synced
    item's), in the caller's transaction."""
    synced_item_id = store.scalar(
        update(Document)
        .where(Document.id == document_id)
        .values(verdict=verdict, **values)
        .returning(Document.synced_item_id)
    )
    if synced_item_id is not None:
        store.execute(
            update(SyncedItem)
            .where(SyncedItem.id == synced_item_id)
            .values(
                verdict=verdict,
                verdict_reason=values.get("verdict_reason"),
                checked_at=values.get("checked_at", SyncedItem.checked_at),
            )
        )
    store.execute(
        update(SearchItem)
        .where(
            SearchItem.kind == SearchItemKind.document_chunk,
            SearchItem.document_id == document_id,
        )
        .values(verdict=verdict)
    )


async def _judge(texts: list[str], source: str) -> jev.Judgement | None:
    """The document's judgement: the first flagged chunk's, else a clean one (None for no
    text). Raises `JevUnavailable`."""
    last = None
    for start in range(0, len(texts), DOCUMENT_BATCH):
        for judgement in await jev.check_all(texts[start : start + DOCUMENT_BATCH], source):
            if judgement.flagged:
                return judgement
            last = judgement
    return last


def check_document(store: Session, document_id: int) -> Verdict | None:
    """Check the document's content with Jev and store its verdict (committing): clean or
    flagged, or `awaiting_check` if Jev couldn't check it. A released document whose content is
    what was released stays released. None if it's gone."""
    doc = store.execute(
        select(
            Document.filename,
            Document.verdict,
            Document.released_by,
            Document.released_content_hash,
            Document.synced_item_id,
            Document.status,
        ).where(Document.id == document_id)
    ).one_or_none()
    if doc is None:
        return None
    texts = _texts(store, document_id)
    hashed = content_hash(texts)
    if doc.verdict == Verdict.released:
        if hashed == doc.released_content_hash:
            return Verdict.released
        _unrelease(store, document_id, doc.filename, doc.released_by)
    try:
        judgement = asyncio.run(_judge(texts, f"document:{doc.filename}"))
    except JevUnavailable as exc:
        log.warning("Jev couldn't check document %s: %s", document_id, exc)
        _set(store, document_id, Verdict.awaiting_check)
        store.commit()
        return Verdict.awaiting_check
    if judgement is None:  # no text at all: nothing that could steer an agent
        verdict, reason, version = Verdict.clean, None, None
    else:
        verdict = Verdict.flagged if judgement.flagged else Verdict.clean
        reason, version = judgement.reason, judgement.version
    _set(
        store,
        document_id,
        verdict,
        verdict_reason=reason,
        verdict_version=version,
        checked_at=func.now(),
        content_hash=hashed,
    )
    if (
        verdict == Verdict.clean
        and doc.verdict != Verdict.clean
        and doc.synced_item_id is not None
        and doc.status == DocumentStatus.ready
    ):
        _synced_document_ingested(store, document_id)
    store.commit()
    return verdict


def _synced_document_ingested(store: Session, document_id: int) -> None:
    """A synced document's document ingested event, now it's found clean (an upload's is written
    as it's ingested, `aios.ingest`), in the caller's transaction."""
    doc = store.execute(
        select(Document.filename, Document.content_type, Document.source_id).where(
            Document.id == document_id
        )
    ).one()
    outbox.emit(
        store,
        EventType.document_ingested,
        {
            "document_id": document_id,
            "name": doc.filename,
            "content_type": doc.content_type,
            "source_id": doc.source_id,
        },
    )


def _unrelease(store: Session, document_id: int, filename: str, released_by: int | None) -> None:
    """Undo a release whose content has changed since (committing): back to `awaiting_check`,
    and the admin who released it is told."""
    _set(
        store,
        document_id,
        Verdict.awaiting_check,
        released_by=None,
        released_at=None,
        released_content_hash=None,
    )
    store.commit()
    if released_by is not None:
        notify_from_store(
            store_id_of(store),
            [released_by],
            RELEASED_DOCUMENT_CHANGED,
            {"document_id": document_id},
        )


def _stale(store: Session, version: str | None) -> list[int]:
    """Documents to check: those awaiting a check, and (given Jev's current `version`) those it
    judged under another; ingested ones only (one still being ingested is checked by it)."""
    due = Document.verdict == Verdict.awaiting_check
    if version is not None:
        due = or_(
            due,
            Document.verdict.in_([Verdict.clean, Verdict.flagged])
            & Document.verdict_version.is_distinct_from(version),
        )
    return list(
        store.scalars(
            select(Document.id)
            .where(Document.status == DocumentStatus.ready, due)
            .order_by(Document.id)
        )
    )


def _changed_releases(store: Session) -> None:
    """Undo each release whose document's content has changed since it was released."""
    released = store.execute(
        select(
            Document.id, Document.filename, Document.released_by, Document.released_content_hash
        ).where(Document.verdict == Verdict.released)
    ).all()
    for doc in released:
        if content_hash(_texts(store, doc.id)) != doc.released_content_hash:
            _unrelease(store, doc.id, doc.filename, doc.released_by)


def sweep(data_store_id: uuid.UUID) -> int:
    """Check the store's stored content again where it's due (see the module): returns how many
    documents were checked. Stops at the first that Jev couldn't check (it's down)."""
    with store_session(data_store_id, "rw") as store:
        _changed_releases(store)
        try:
            version = asyncio.run(jev.classifier().version())
        except JevUnavailable:
            version = None
        checked = 0
        for document_id in _stale(store, version):
            verdict = check_document(store, document_id)
            if verdict == Verdict.awaiting_check:
                return checked
            checked += 1
        for item_id in _stale_items(store, version):
            verdict = check_synced_item(store, item_id)
            store.commit()
            if verdict == Verdict.awaiting_check:
                break
            checked += 1
        return checked


# --- Synced message items ---------------------------------------------------------------------


def _set_item(store: Session, item_id: int, verdict: Verdict, **values) -> None:
    """The synced item's verdict, and its search items', in the caller's transaction."""
    store.execute(
        update(SyncedItem).where(SyncedItem.id == item_id).values(verdict=verdict, **values)
    )
    store.execute(
        update(SearchItem)
        .where(SearchItem.kind == SearchItemKind.source_item, SearchItem.synced_item_id == item_id)
        .values(verdict=verdict)
    )


def judge_text(text: str, source: str) -> jev.Judgement | None:
    """Jev's judgement of a synced item's text (None for no text: nothing could steer an agent).
    Raises `JevUnavailable`."""
    if not text.strip():
        return None
    return asyncio.run(jev.check(text, source))


def check_synced_item(store: Session, item_id: int) -> Verdict | None:
    """Check a message item's text with Jev and store its verdict, on it and its search items,
    in the caller's transaction (uncommitted): clean or flagged, or `awaiting_check` if Jev
    couldn't check it. A released item is left released (its sync undoes a release when its
    content changes). None if it's gone, or isn't a message item."""
    item = store.execute(
        select(SyncedItem.kind, SyncedItem.text, SyncedItem.verdict, Source.name)
        .join(Source, Source.id == SyncedItem.source_id)
        .where(SyncedItem.id == item_id)
    ).one_or_none()
    if item is None or item.kind != SyncedItemKind.message:
        return None
    if item.verdict == Verdict.released:
        return Verdict.released
    try:
        judgement = judge_text(item.text or "", f"source:{item.name}")
    except JevUnavailable as exc:
        log.warning("Jev couldn't check synced item %s: %s", item_id, exc)
        _set_item(store, item_id, Verdict.awaiting_check)
        return Verdict.awaiting_check
    if judgement is None:
        verdict, reason, version = Verdict.clean, None, None
    else:
        verdict = Verdict.flagged if judgement.flagged else Verdict.clean
        reason, version = judgement.reason, judgement.version
    _set_item(
        store,
        item_id,
        verdict,
        verdict_reason=reason,
        verdict_version=version,
        checked_at=func.now(),
    )
    return verdict


def unrelease_item(store: Session, item_id: int) -> None:
    """Undo a message item's release, its content having changed at its source: back to
    `awaiting_check`, in the caller's transaction. Its sync tells the admins."""
    _set_item(store, item_id, Verdict.awaiting_check, released_by=None, released_at=None)


def _stale_items(store: Session, version: str | None) -> list[int]:
    """Message items to check: awaiting a check, or judged by another version of Jev."""
    due = SyncedItem.verdict == Verdict.awaiting_check
    if version is not None:
        due = or_(
            due,
            SyncedItem.verdict.in_([Verdict.clean, Verdict.flagged])
            & SyncedItem.verdict_version.is_distinct_from(version),
        )
    return list(
        store.scalars(
            select(SyncedItem.id)
            .where(SyncedItem.kind == SyncedItemKind.message, due)
            .order_by(SyncedItem.id)
        )
    )


# --- The flagged list ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Flagged:
    id: int
    filename: str
    reason: str | None
    checked_at: datetime | None
    uploaded_by: int | None
    created_at: datetime


def flagged(store: Session) -> list[Flagged]:
    """The store's flagged documents, the most recently checked first."""
    rows = store.execute(
        select(
            Document.id,
            Document.filename,
            Document.verdict_reason,
            Document.checked_at,
            Document.uploaded_by,
            Document.created_at,
        )
        .where(Document.verdict == Verdict.flagged)
        .order_by(Document.checked_at.desc().nulls_last(), Document.id.desc())
    ).all()
    return [
        Flagged(r.id, r.filename, r.verdict_reason, r.checked_at, r.uploaded_by, r.created_at)
        for r in rows
    ]


class NotFlagged(Exception):
    """Only a flagged document can be released."""


def release(store: Session, document_id: int, admin_id: int) -> Document | None:
    """Release the flagged document (committing, with its audit entry): it's found by search and
    reaches context from now on, still wrapped as untrusted. None if there's no such document;
    raises `NotFlagged` if it isn't flagged."""
    doc = store.execute(
        select(Document).where(Document.id == document_id).with_for_update()
    ).scalar_one_or_none()
    if doc is None:
        return None
    if doc.verdict != Verdict.flagged:
        raise NotFlagged
    reason = doc.verdict_reason
    _set(
        store,
        document_id,
        Verdict.released,
        released_by=admin_id,
        released_at=func.now(),
        released_content_hash=content_hash(_texts(store, document_id)),
    )
    audit.record(
        store,
        audit.person(admin_id),
        "document.released",
        "document",
        document_id,
        {"filename": doc.filename, "reason": reason},
    )
    store.commit()
    store.refresh(doc)
    return doc


@dataclass(frozen=True)
class FlaggedItem:
    id: int
    title: str
    url: str | None
    source_id: int
    source_name: str
    reason: str | None
    checked_at: datetime | None
    created_at: datetime


def flagged_items(store: Session) -> list[FlaggedItem]:
    """The store's flagged synced message items (a file item is on the list as its document),
    the most recently checked first."""
    rows = store.execute(
        select(
            SyncedItem.id,
            SyncedItem.title,
            SyncedItem.url,
            SyncedItem.source_id,
            Source.name,
            SyncedItem.verdict_reason,
            SyncedItem.checked_at,
            SyncedItem.created_at,
        )
        .join(Source, Source.id == SyncedItem.source_id)
        .where(SyncedItem.verdict == Verdict.flagged, SyncedItem.kind == SyncedItemKind.message)
        .order_by(SyncedItem.checked_at.desc().nulls_last(), SyncedItem.id.desc())
    ).all()
    return [FlaggedItem(*r) for r in rows]


def release_item(store: Session, item_id: int, admin_id: int) -> SyncedItem | None:
    """Release a flagged synced message item (committing, with its audit entry): it's found by
    search and reaches context from now on, still wrapped as untrusted, until its content
    changes at its source. Fires no event. None if there's no such item; raises `NotFlagged` if
    it isn't flagged."""
    item = store.execute(
        select(SyncedItem)
        .where(SyncedItem.id == item_id, SyncedItem.kind == SyncedItemKind.message)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        return None
    if item.verdict != Verdict.flagged:
        raise NotFlagged
    reason = item.verdict_reason
    _set_item(store, item_id, Verdict.released, released_by=admin_id, released_at=func.now())
    audit.record(
        store,
        audit.person(admin_id),
        "synced_item.released",
        "synced_item",
        item_id,
        {"title": item.title, "source_id": item.source_id, "reason": reason},
    )
    store.commit()
    store.refresh(item)
    return item
