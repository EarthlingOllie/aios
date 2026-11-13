"""The organisation's sources (`aios.sources`, `aios.sync`): every member sees them, their health
and their recent syncs; only admins add, change, sync, pause, resume or remove them, and Uploads
can't be changed (409).

Admins add sources of these kinds (`source-kinds` says which this deployment offers, and why any
isn't):

- **URL** (`aios.url_sources`): a web page fetched on its interval. An address aios may not
  reach (`aios.url_fetch`) is a 422, "This address isn't reachable from aios".
- **Webhook** (`aios.push_sources`): made, its URL and signing secret are answered, the secret
  only then and when it's rotated (`POST .../secret`), which changes the URL too.
- **Email** (`aios.push_sources`): made, its address is answered, only then and when it's
  rotated, which cuts the old one off. It accepts mail only from its allowed senders (none at
  first). Unavailable (422) without an inbound mail domain.
- **A provider's** (the test provider, `aios.source_providers`).

Syncs themselves aren't reachable over HTTP: Sync now queues one on the run service's `sources`
lane (202), or answers 409 while one runs. Webhook and email sources are pushed to, and never
sync (409).
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from aios import audit, push_sources, source_providers, sync, url_fetch, webhooks
from aios.auth import CurrentDataStore, CurrentOrganisation, CurrentUser
from aios.config import get_settings
from aios.datastores import store_id_of, store_session
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
PUSHED = "This source is pushed to: there's nothing to sync."
NO_SECRET = "Only a webhook or email source has a secret or address to rotate."
CONNECTORS_SOURCE = (
    "This source is its connector's: remove the connector to stop it, keeping or deleting what "
    "it synced."
)


class SkippedFileOut(BaseModel):
    name: str
    reason: str


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
    # The files it left out (unsupported, or too big), each with why: the first 200.
    skipped_files: list[SkippedFileOut] = []
    error: str | None


class SourceOut(BaseModel):
    id: int
    kind: SourceKind
    name: str
    # A URL source's page.
    url: str | None = None
    # An email source's allowed senders (addresses, and domains as `@example.com`).
    allowed_senders: list[str] | None = None
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
    # A webhook source's endpoint.
    webhook_url: str | None = None
    # Only as it's made, or rotated: a webhook source's signing secret, an email source's
    # address.
    webhook_secret: str | None = None
    email_address: str | None = None


class KindOut(BaseModel):
    kind: Literal["url", "webhook", "email"]
    # Why it can't be added here; null if it can.
    unavailable: str | None


class ProviderOut(BaseModel):
    provider: str
    # Why it can't be used here; null if it can.
    unavailable: str | None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    # A provider's (with `provider`), or a URL, webhook or email source.
    kind: Literal["connector", "url", "webhook", "email"] = "connector"
    provider: str | None = Field(None, min_length=1, max_length=64)
    # The test provider's feed (`aios.source_providers.test`).
    feed: str | None = Field(None, max_length=255)
    # A URL source's page.
    url: str | None = Field(None, max_length=2048)
    # An email source's allowed senders.
    allowed_senders: list[str] = Field(default_factory=list, max_length=200)
    interval_seconds: int | None = Field(None, ge=1)


class SourceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, pattern=r"\S")
    # Pause (true) or resume (false).
    paused: bool | None = None
    interval_seconds: int | None = Field(None, ge=1)
    # A URL source's page.
    url: str | None = Field(None, max_length=2048)
    # An email source's allowed senders.
    allowed_senders: list[str] | None = Field(None, max_length=200)


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
    settings = source.settings or {}
    return SourceOut(
        id=source.id,
        kind=source.kind,
        name=source.name,
        url=settings.get("url") if source.kind == SourceKind.url else None,
        allowed_senders=settings.get("allowed_senders", [])
        if source.kind == SourceKind.email
        else None,
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
    return SourceDetailOut(
        **out.model_dump(),
        syncs=[_sync_out(s) for s in syncs],
        webhook_url=_webhook_url(store, source),
    )


def _webhook_url(store: Session, source: Source) -> str | None:
    """A webhook source's endpoint, read from its secret by the read-write role (the secret is
    on the deny list)."""
    if source.kind != SourceKind.webhook:
        return None
    with store_session(store_id_of(store), "rw") as rw:
        secret = rw.scalar(select(Source.secret).where(Source.id == source.id))
    return webhooks.url(webhooks.decrypt(secret)) if secret else None


def _changeable(store: Session, source_id: int) -> Source:
    """The source, locked, to change or remove: a 404 if it isn't the organisation's, a 409 if
    it's Uploads."""
    source = store.scalar(select(Source).where(Source.id == source_id).with_for_update())
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if source.kind == SourceKind.upload:
        raise HTTPException(status.HTTP_409_CONFLICT, UPLOADS_FIXED)
    return source


def _check_url(url: str | None) -> str:
    """The URL, if aios may reach it: else a 422, "This address isn't reachable from aios"."""
    url = (url or "").strip()
    if not url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A URL source needs a URL.")
    try:
        url_fetch.check(url)
    except url_fetch.Unreachable:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, url_fetch.UNREACHABLE) from None
    return url


def _check_senders(given: list[str]) -> list[str]:
    try:
        return push_sources.checked_senders(given)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


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


@router.get("/source-kinds", response_model=list[KindOut], dependencies=[allow(Action.read)])
def list_kinds():
    """The kinds of source an admin can add here besides a provider's (URL, webhook and email),
    and why any can't be: email needs an inbound mail service."""
    return [
        KindOut(kind="url", unavailable=None),
        KindOut(kind="webhook", unavailable=None),
        KindOut(kind="email", unavailable=push_sources.email_unavailable()),
    ]


def _settings(body: SourceCreate) -> tuple[SourceKind, dict]:
    """The new source's kind and settings, checked (422s)."""
    if body.kind == "url":
        return SourceKind.url, {"url": _check_url(body.url)}
    if body.kind == "webhook":
        return SourceKind.webhook, {}
    if body.kind == "email":
        why = push_sources.email_unavailable()
        if why is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, why)
        return SourceKind.email, {"allowed_senders": _check_senders(body.allowed_senders)}
    provider = body.provider or ""
    why = source_providers.available().get(provider, f"There's no provider {provider!r}.")
    if why is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, why)
    settings = {"provider": provider}
    if body.feed:
        settings["feed"] = body.feed
    return SourceKind.connector, settings


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
    """Add a source (see the module). One that syncs has its first sync queued at once. 422 for
    a kind or provider that isn't offered here, a URL aios may not reach, a bad sender, or an
    interval under the minimum."""
    kind, settings = _settings(body)
    pushed = kind in sync.PUSHED
    if not pushed:
        _check_interval(body.interval_seconds)
    with store_session(store_id, "rw") as store:
        source = Source(
            kind=kind,
            name=body.name,
            settings=settings,
            interval_seconds=None
            if pushed
            else body.interval_seconds or get_settings().sync_interval_seconds,
            created_by=user.id,
        )
        store.add(source)
        store.flush()
        secret = address = None
        if kind == SourceKind.webhook:
            secret = push_sources.new_webhook_secret(source)
            webhooks.index(store_id, org.id, None, source.token_hash, source_id=source.id)
        elif kind == SourceKind.email:
            address = push_sources.new_address(source)
            push_sources.index_address(store_id, org.id, source.id, source.token_hash)
        audit.configured(
            store,
            user.id,
            "source",
            source,
            "created",
            source_kind=kind.value,
            **_audited(settings),
        )
        store.commit()
        source_id = source.id
        if not pushed:
            enqueue_sync(org.id, store_id, source_id, SyncKind.incremental)
        out = _one(store, source_id)
    out.webhook_secret, out.email_address = secret, address
    return out


def _audited(settings: dict) -> dict:
    return {k: v for k, v in settings.items() if k in ("provider", "url", "allowed_senders")}


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
    """Rename a source, set how often it syncs (422 under the minimum), a URL source's page, an
    email source's allowed senders, or pause or resume it. Uploads can't be (409)."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        _check_interval(body.interval_seconds)
        if body.name is not None:
            source.name = body.name
        if body.url is not None and source.kind == SourceKind.url:
            url = _check_url(body.url)
            if url != (source.settings or {}).get("url"):
                source.settings = {**(source.settings or {}), "url": url}
                # Fetched afresh: the next sync lists it whole, removing the old page.
                source.cursor = source.last_reconciled_at = source.next_sync_at = None
        if body.allowed_senders is not None and source.kind == SourceKind.email:
            source.settings = {
                **(source.settings or {}),
                "allowed_senders": _check_senders(body.allowed_senders),
            }
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
        if source.kind in sync.PUSHED:
            raise HTTPException(status.HTTP_409_CONFLICT, PUSHED)
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
    """Mark a source for removal; it stops syncing (or taking what's pushed to it) at once.
    Uploads can't be removed (409)."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        if source.connector_id is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, CONNECTORS_SOURCE)
        source.status = SourceStatus.removing
        dropped = _drop_token(source)
        audit.configured(store, user.id, "source", source, "removed")
        store.commit()
    _unindex(dropped)


def _drop_token(source: Source) -> tuple[SourceKind, str] | None:
    """Take the webhook or email source's token (or address) off it: what to unindex once the
    store commits."""
    if source.token_hash is None:
        return None
    dropped = (source.kind, source.token_hash)
    source.token_hash = None
    return dropped


def _unindex(dropped: tuple[SourceKind, str] | None) -> None:
    if dropped is None:
        return
    kind, hashed = dropped
    if kind == SourceKind.webhook:
        webhooks.unindex([hashed])
    else:
        push_sources.unindex_address([hashed])


@router.post(
    "/sources/{source_id}/secret",
    response_model=SourceDetailOut,
    dependencies=[allow(Action.sources)],
)
def rotate_secret(
    source_id: DbId, user: CurrentUser, org: CurrentOrganisation, store_id: CurrentDataStore
):
    """Regenerate a webhook source's secret (and so its URL) or an email source's address,
    answered this once; the old one leads nowhere from now. 409 for other kinds, or a source
    being removed."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        if source.kind not in sync.PUSHED:
            raise HTTPException(status.HTTP_409_CONFLICT, NO_SECRET)
        if source.status == SourceStatus.removing:
            raise HTTPException(status.HTTP_409_CONFLICT, REMOVING)
        dropped = _drop_token(source)
        secret = address = None
        if source.kind == SourceKind.webhook:
            secret = push_sources.new_webhook_secret(source)
            webhooks.index(store_id, org.id, None, source.token_hash, source_id=source.id)
        else:
            if (why := push_sources.email_unavailable()) is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, why)
            address = push_sources.new_address(source)
            push_sources.index_address(store_id, org.id, source.id, source.token_hash)
        audit.configured(store, user.id, "source", source, "secret_rotated")
        store.commit()
        out = _one(store, source_id)
    _unindex(dropped)
    out.webhook_secret, out.email_address = secret, address
    return out
