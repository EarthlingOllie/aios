"""Each data store's storage cap: its database's size (`pg_database_size`) and its documents'
files' bytes, against `data_store_db_quota_bytes` and `data_store_files_quota_bytes`.

Over either cap, uploads and `write_data`'s inserts and updates are refused; reads, searches and
deletes carry on. The checks measure the store as the write comes in (`upload_refusal`,
`write_refusal`), so a store not yet measured, or filled since, is held to its cap all the same.

What each store uses is also recorded on its `data_stores` row, for the Data page: both sizes by
`measure_all`, on a schedule (`aios.worker`), and the documents' bytes again after each upload
and delete (`record_files`), so room made shows straight away.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores.connections import dispose_engine, store_session
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus

log = logging.getLogger(__name__)

# Either login role may ask (each may connect to its store's database).
_DB_BYTES = text("SELECT pg_database_size(current_database())")
_FILES_BYTES = text("SELECT coalesce(sum(size_bytes), 0) FROM aios.documents")


@dataclass(frozen=True)
class Usage:
    """What a store uses, as last recorded (None until it's first measured), and its caps."""

    db_bytes: int | None
    db_quota_bytes: int
    files_bytes: int | None
    files_quota_bytes: int
    measured_at: datetime | None


_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(n: int) -> str:
    """A size for people, as the Data page writes it (`formatBytes`): "512 B", "1.5 KB",
    "20 MB" (binary units)."""
    unit, value = 0, float(n)
    # Up a unit at 1024, or at what would round to 1024 (1023.95 KB is "1.0 MB").
    while unit < len(_UNITS) - 1 and value >= (1024 if unit == 0 else 1023.95):
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{n} B"
    # Halves up, as JavaScript's toFixed and Math.round (Python's rounding goes to the even one),
    # on the float's exact value, as they do.
    places = Decimal("0.1") if value < 9.95 else Decimal(1)
    return f"{Decimal(value).quantize(places, ROUND_HALF_UP)} {_UNITS[unit]}"


def _of(used: int, quota: int) -> str:
    return f"{format_bytes(used)} of the {format_bytes(quota)} allowed"


def upload_refusal(store: Session, filename: str, size: int) -> str | None:
    """Why an upload of `size` bytes is refused, for the user; None if there's room. `store` is
    a session on the store."""
    settings = get_settings()
    db_quota, files_quota = (
        settings.data_store_db_quota_bytes,
        settings.data_store_files_quota_bytes,
    )
    # Ingesting it writes its chunks to the database.
    db_bytes = store.scalar(_DB_BYTES)
    if db_bytes >= db_quota:
        return (
            f"Your data store is full: its database uses {_of(db_bytes, db_quota)}, so documents"
            " can't be uploaded."
        )
    files_bytes = store.scalar(_FILES_BYTES)
    if files_bytes >= files_quota:
        return (
            f"Your data store is full: its documents use {_of(files_bytes, files_quota)}."
            " Delete some to make room."
        )
    if files_bytes + size > files_quota:
        return (
            f"There's no room for {filename}: your documents use {_of(files_bytes, files_quota)},"
            f" and it's {format_bytes(size)}. Delete some to make room."
        )
    return None


def write_refusal(store: Session) -> str | None:
    """Why `write_data` refuses an insert or update, for the model (which tells the user); None
    if there's room. `store` is a session on the store."""
    settings = get_settings()
    db_quota, files_quota = (
        settings.data_store_db_quota_bytes,
        settings.data_store_files_quota_bytes,
    )
    db_bytes = store.scalar(_DB_BYTES)
    if db_bytes >= db_quota:
        return (
            f"The user's data store is full: its database uses {_of(db_bytes, db_quota)}, so rows"
            " can't be inserted or updated. Reading and deleting rows still work. Tell the user:"
            " the Data page shows their usage."
        )
    files_bytes = store.scalar(_FILES_BYTES)
    if files_bytes >= files_quota:
        return (
            f"The user's data store is full: its documents use {_of(files_bytes, files_quota)},"
            " so rows can't be inserted or updated. Reading and deleting rows still work. Tell"
            " the user: deleting documents on the Data page makes room."
        )
    return None


def _record(data_store_id: uuid.UUID, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(DataStore).where(DataStore.id == data_store_id).values(**values))
        db.commit()


def record_files(data_store_id: uuid.UUID, store: Session) -> None:
    """Record the store's documents' bytes now, after an upload or delete. `store` is a session
    on it."""
    _record(data_store_id, files_bytes=store.scalar(_FILES_BYTES))


def measure(data_store_id: uuid.UUID) -> None:
    """Measure what the store uses now, and record it on its row."""
    with store_session(data_store_id, "ro") as store:
        db_bytes, files_bytes = store.scalar(_DB_BYTES), store.scalar(_FILES_BYTES)
    _record(data_store_id, db_bytes=db_bytes, files_bytes=files_bytes, usage_measured_at=func.now())


def measure_all() -> None:
    """Measure every ready store, one at a time. One that fails is logged, and the rest are
    measured all the same; it keeps its last measurement."""
    with SessionLocal() as db:
        ids = db.scalars(
            select(DataStore.id).where(DataStore.status == DataStoreStatus.ready)
        ).all()
    for store_id in ids:
        try:
            measure(store_id)
        except SoftTimeLimitExceeded:
            # The run is out of time (not this store): it stops, and the next carries on.
            raise
        except Exception:
            log.exception("Couldn't measure data store %s", store_id)
        finally:
            # Not kept for the next run, minutes away: a connection to every store adds up.
            dispose_engine(store_id, "ro")


def usage(data_store_id: uuid.UUID) -> Usage:
    """The store's usage, as last recorded, against the configured caps."""
    with SessionLocal() as db:
        row = db.execute(
            select(DataStore.db_bytes, DataStore.files_bytes, DataStore.usage_measured_at).where(
                DataStore.id == data_store_id
            )
        ).one()
    settings = get_settings()
    return Usage(
        db_bytes=row.db_bytes,
        db_quota_bytes=settings.data_store_db_quota_bytes,
        files_bytes=row.files_bytes,
        files_quota_bytes=settings.data_store_files_quota_bytes,
        measured_at=row.usage_measured_at,
    )
