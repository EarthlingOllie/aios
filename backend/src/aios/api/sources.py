"""The organisation's sources (`aios.sources`, `aios.sync`): every member sees them, their health
and their recent syncs; only admins add, change, sync, pause, resume or remove them, and Uploads
can't be changed (409).

Syncs themselves aren't reachable over HTTP: Sync now queues one on the run service's `sources`
lane (202), or answers 409 while one runs.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from aios import audit, source_providers, sync
from aios.auth import CurrentDataStore, CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Document,
    Source,
    SourceKind,
    SourceStatus,
    SourceSync,
    SyncedItem,
    SyncKind,
    SyncStatus,
)
from aios.db import DbId
from aios.permissions import Action, allow
from aios.run_service.work import enqueue_sync
from aios.sources import UPLOADS_FIXED

router = APIRouter(prefix="/api")

# How many of a source's syncs its page shows.
RECENT_SYNCS = 20

ALREADY_SYNCING = "This source is syncing now. Try again once it's finished."
PAUSED = "This source is paused. Resume it to sync it."
REMOVING = "This source is being removed."


class SyncOut(BaseModel):
    id: int
    kind: SyncKind
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    added: int
    updated: int
    removed: int
    skipped: int
    dropped: int
    # Its source's items still waiting for Jev after it.
    awaiting_check: int
    storage_full: bool
    error: str | None


class SourceOut(BaseModel):
    id: int
    kind: SourceKind
    name: str
    status: SourceStatus
    # healthy, delayed, failing or needs_reconnecting (`aios.sync.health`).
    health: str
    # The provider it syncs from (a connector's or the test provider); null for Uploads.
    provider: str | None
    # How often it syncs, in seconds; null for Uploads.
    interval_seconds: int | None
    # The documents it has brought in, and its synced items (files and messages).
    document_count: int
    item_count: int
    last_sync: SyncOut | None
    next_sync_at: datetime | None
    created_at: datetime


class SourceDetailOut(SourceOut):
    # Its most recent syncs, newest first.
    syncs: list[SyncOut]


class ProviderOut(BaseModel):
    provider: str
    # Why it can't be used here; null if it can.
    unavailable: str | None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    provider: str = Field(min_length=1, max_length=64)
    # The test provider's feed (`aios.source_providers.test`).
    feed: str | None = Field(None, max_length=255)
    interval_seconds: int | None = Field(None, ge=1)


class SourceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, pattern=r"\S")
    # Pause (true) or resume (false).
    paused: bool | None = None
    interval_seconds: int | None = Field(None, ge=1)


class SyncRequest(BaseModel):
    # A full resync (everything in scope, removing what's gone) rather than the changes since
    # its last sync.
    full: bool = False


def _sync_out(row: SourceSync | None) -> SyncOut | None:
    return None if row is None else SyncOut.model_validate(row, from_attributes=True)


def _query():
    documents = (
        select(func.count(Document.id))
        .where(Document.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    items = (
        select(func.count(SyncedItem.id))
        .where(SyncedItem.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    return select(Source, documents, items).order_by(Source.id)


def _last(store: Session, source_ids: list[int]) -> tuple[dict, dict]:
    """Each source's last sync, and when its last successful one started."""
    if not source_ids:
        return {}, {}
    last = {
        s.source_id: s
        for s in store.scalars(
            select(SourceSync)
            .where(SourceSync.source_id.in_(source_ids))
            .ext(distinct_on(SourceSync.source_id))
            .order_by(SourceSync.source_id, SourceSync.started_at.desc(), SourceSync.id.desc())
        )
    }
    succeeded = dict(
        store.execute(
            select(SourceSync.source_id, func.max(SourceSync.started_at))
            .where(SourceSync.source_id.in_(source_ids), SourceSync.status == SyncStatus.succeeded)
            .group_by(SourceSync.source_id)
        ).all()
    )
    return last, succeeded


def _out(source: Source, documents: int, items: int, last: SourceSync | None, ok) -> SourceOut:
    return SourceOut(
        id=source.id,
        kind=source.kind,
        name=source.name,
        status=source.status,
        health=sync.health(source, last, ok),
        provider=(source.settings or {}).get("provider"),
        interval_seconds=source.interval_seconds,
        document_count=documents,
        item_count=items,
        last_sync=_sync_out(last),
        next_sync_at=source.next_sync_at,
        created_at=source.created_at,
    )


def sources_out(store: Session) -> list[SourceOut]:
    """Every source, Uploads first, with its health, last sync and counts (as Insights shows
    them too, `aios.insights`)."""
    rows = store.execute(_query()).all()
    last, ok = _last(store, [s.id for s, _, _ in rows])
    return [_out(s, d, i, last.get(s.id), ok.get(s.id)) for s, d, i in rows]


def _one(store: Session, source_id: int) -> SourceDetailOut:
    row = store.execute(_query().where(Source.id == source_id)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    source, documents, items = row
    last, ok = _last(store, [source_id])
    syncs = store.scalars(
        select(SourceSync)
        .where(SourceSync.source_id == source_id)
        .order_by(SourceSync.started_at.desc(), SourceSync.id.desc())
        .limit(RECENT_SYNCS)
    ).all()
    out = _out(source, documents, items, last.get(source_id), ok.get(source_id))
    return SourceDetailOut(**out.model_dump(), syncs=[_sync_out(s) for s in syncs])


def _changeable(store: Session, source_id: int) -> Source:
    """The source, locked, to change or remove: a 404 if it isn't the organisation's, a 409 if
    it's Uploads."""
    source = store.scalar(select(Source).where(Source.id == source_id).with_for_update())
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if source.kind == SourceKind.upload:
        raise HTTPException(status.HTTP_409_CONFLICT, UPLOADS_FIXED)
    return source


def _check_interval(seconds: int | None) -> None:
    minimum = get_settings().sync_min_interval_seconds
    if seconds is not None and seconds < minimum:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A source can sync at most every {minimum // 60} minutes ({minimum} seconds).",
        )


@router.get("/sources", response_model=list[SourceOut], dependencies=[allow(Action.read)])
def list_sources(store_id: CurrentDataStore):
    """The organisation's sources, Uploads first, with each one's health, last sync and how many
    documents and items it has brought in."""
    with store_session(store_id, "ro") as store:
        return sources_out(store)


@router.get(
    "/source-providers", response_model=list[ProviderOut], dependencies=[allow(Action.read)]
)
def list_providers():
    """The providers a source can be added with here, and why any can't."""
    return [
        ProviderOut(provider=p, unavailable=why) for p, why in source_providers.available().items()
    ]


@router.post(
    "/sources",
    response_model=SourceDetailOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.sources)],
)
def create_source(
    body: SourceCreate,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
):
    """Add a source that syncs from a provider; its first sync is queued at once. 422 for a
    provider that isn't offered here, or an interval under the minimum."""
    why = source_providers.available().get(body.provider, f"There's no provider {body.provider!r}.")
    if why is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, why)
    _check_interval(body.interval_seconds)
    settings = {"provider": body.provider}
    if body.feed:
        settings["feed"] = body.feed
    with store_session(store_id, "rw") as store:
        source = Source(
            kind=SourceKind.connector,
            name=body.name,
            settings=settings,
            interval_seconds=body.interval_seconds or get_settings().sync_interval_seconds,
            created_by=user.id,
        )
        store.add(source)
        store.flush()
        audit.configured(store, user.id, "source", source, "created", provider=body.provider)
        store.commit()
        source_id = source.id
        enqueue_sync(org.id, store_id, source_id, SyncKind.incremental)
        return _one(store, source_id)


@router.get(
    "/sources/{source_id}", response_model=SourceDetailOut, dependencies=[allow(Action.read)]
)
def get_source(source_id: DbId, store_id: CurrentDataStore):
    """The source, with its recent syncs, newest first."""
    with store_session(store_id, "ro") as store:
        return _one(store, source_id)


@router.patch(
    "/sources/{source_id}", response_model=SourceDetailOut, dependencies=[allow(Action.sources)]
)
def update_source(
    source_id: DbId, body: SourceUpdate, user: CurrentUser, store_id: CurrentDataStore
):
    """Rename a source, set how often it syncs (422 under the minimum), or pause or resume it.
    Uploads can't be (409)."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        _check_interval(body.interval_seconds)
        if body.name is not None:
            source.name = body.name
        if body.interval_seconds is not None:
            source.interval_seconds = body.interval_seconds
        if body.paused is True and source.status != SourceStatus.removing:
            source.status = SourceStatus.paused
        elif body.paused is False and source.status == SourceStatus.paused:
            source.status = SourceStatus.active
            source.next_sync_at = None  # due at once
        changed = body.model_dump(exclude_none=True)
        audit.configured(store, user.id, "source", source, "updated", **changed)
        store.commit()
        return _one(store, source_id)


@router.post(
    "/sources/{source_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[allow(Action.sources)],
)
def sync_source(
    source_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    store_id: CurrentDataStore,
    body: SyncRequest | None = None,
) -> None:
    """Sync the source now: the changes since its last sync, or (`full`) a full resync. Queued
    on the run service (202); 409 while a sync of it runs, or if it's Uploads, paused or being
    removed."""
    full = body is not None and body.full
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        if source.status == SourceStatus.paused:
            raise HTTPException(status.HTTP_409_CONFLICT, PAUSED)
        if source.status == SourceStatus.removing:
            raise HTTPException(status.HTTP_409_CONFLICT, REMOVING)
        if sync.running(store, source_id):
            raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_SYNCING)
        audit.configured(store, user.id, "source", source, "synced", full=full)
        store.commit()
    enqueue_sync(org.id, store_id, source_id, SyncKind.full if full else SyncKind.manual)


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[allow(Action.sources)],
)
def remove_source(source_id: DbId, user: CurrentUser, store_id: CurrentDataStore) -> None:
    """Mark a source for removal; it stops syncing at once. Uploads can't be removed (409)."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        source.status = SourceStatus.removing
        audit.configured(store, user.id, "source", source, "removed")
        store.commit()
