"""The organisation's sources (`aios.sources`): every member sees them; only admins change or
remove them, and Uploads can't be (409)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.auth import CurrentDataStore
from aios.datastores import store_session
from aios.datastores.models import Document, Source, SourceKind, SourceStatus
from aios.db import DbId
from aios.permissions import Action, allow
from aios.sources import UPLOADS_FIXED

router = APIRouter(prefix="/api")


class SourceOut(BaseModel):
    id: int
    kind: SourceKind
    name: str
    status: SourceStatus
    # The documents it has brought in.
    document_count: int
    created_at: datetime


class SourceUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, pattern=r"\S")
    # Pause (true) or resume (false).
    paused: bool | None = None


def _query():
    count = (
        select(func.count(Document.id))
        .where(Document.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    return select(Source, count).order_by(Source.id)


def _out(source: Source, documents: int) -> SourceOut:
    return SourceOut(
        id=source.id,
        kind=source.kind,
        name=source.name,
        status=source.status,
        document_count=documents,
        created_at=source.created_at,
    )


def _one(store: Session, source_id: int) -> SourceOut:
    row = store.execute(_query().where(Source.id == source_id)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _out(*row)


def _changeable(store: Session, source_id: int) -> Source:
    """The source, locked, to change or remove: a 404 if it isn't the organisation's, a 409 if
    it's Uploads."""
    source = store.scalar(select(Source).where(Source.id == source_id).with_for_update())
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if source.kind == SourceKind.upload:
        raise HTTPException(status.HTTP_409_CONFLICT, UPLOADS_FIXED)
    return source


@router.get("/sources", response_model=list[SourceOut], dependencies=[allow(Action.read)])
def list_sources(store_id: CurrentDataStore):
    """The organisation's sources, Uploads first, with how many documents each has brought in."""
    with store_session(store_id, "ro") as store:
        return [_out(*row) for row in store.execute(_query()).all()]


@router.get("/sources/{source_id}", response_model=SourceOut, dependencies=[allow(Action.read)])
def get_source(source_id: DbId, store_id: CurrentDataStore):
    with store_session(store_id, "ro") as store:
        return _one(store, source_id)


@router.patch(
    "/sources/{source_id}", response_model=SourceOut, dependencies=[allow(Action.sources)]
)
def update_source(source_id: DbId, body: SourceUpdate, store_id: CurrentDataStore):
    """Rename, pause or resume a source. Uploads can't be (409)."""
    with store_session(store_id, "rw") as store:
        source = _changeable(store, source_id)
        if body.name is not None:
            source.name = body.name
        if body.paused is True and source.status != SourceStatus.removing:
            source.status = SourceStatus.paused
        elif body.paused is False and source.status == SourceStatus.paused:
            source.status = SourceStatus.active
        store.commit()
        return _one(store, source_id)


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[allow(Action.sources)],
)
def remove_source(source_id: DbId, store_id: CurrentDataStore) -> None:
    """Mark a source for removal; it stops syncing at once. Uploads can't be removed (409)."""
    with store_session(store_id, "rw") as store:
        _changeable(store, source_id).status = SourceStatus.removing
        store.commit()
