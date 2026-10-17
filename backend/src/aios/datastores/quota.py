"""Each data store's storage cap: its database's size (`pg_database_size`) and its documents'
files' bytes, against `data_store_db_quota_bytes` and `data_store_files_quota_bytes`.

`measure_all`, on a schedule (`aios.worker`), records what each ready store uses on its
`data_stores` row; the checks read that record, so a store is over its cap from the first
measurement past it until one back under it (its owner deleted something). Over either cap,
uploads and `write_data`'s inserts and updates are refused; reads, searches and deletes carry on.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus

log = logging.getLogger(__name__)

# One query, as the store's read-only role (which may connect to its own database).
_MEASURE = text(
    "SELECT pg_database_size(current_database()),"
    " (SELECT coalesce(sum(size_bytes), 0) FROM aios.documents)"
)


@dataclass(frozen=True)
class Usage:
    """What a store uses, as last measured (None until it's first measured), and its caps."""

    db_bytes: int | None
    db_quota_bytes: int
    files_bytes: int | None
    files_quota_bytes: int
    measured_at: datetime | None

    @property
    def db_full(self) -> bool:
        return self.db_bytes is not None and self.db_bytes >= self.db_quota_bytes

    @property
    def files_full(self) -> bool:
        return self.files_bytes is not None and self.files_bytes >= self.files_quota_bytes


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
    # Halves up, as JavaScript's Math.round (Python's round goes to the even one).
    whole = math.floor(value + 0.5)
    return f"{value:.1f} {_UNITS[unit]}" if value < 9.95 else f"{whole} {_UNITS[unit]}"


def files_full(usage: Usage) -> str:
    """Why an upload is refused, for the user."""
    return (
        f"Your data store is full: its documents use {format_bytes(usage.files_bytes or 0)} of"
        f" the {format_bytes(usage.files_quota_bytes)} allowed. Delete some to upload more."
    )


def db_full(usage: Usage) -> str:
    """Why `write_data` refuses an insert or update, for the model (which tells the user)."""
    return (
        f"The user's data store is full: its database uses {format_bytes(usage.db_bytes or 0)}"
        f" of the {format_bytes(usage.db_quota_bytes)} allowed, so rows can't be inserted or"
        " updated. Reading and deleting rows still work. Tell the user: the Data page shows"
        " their usage."
    )


def measure(data_store_id: uuid.UUID) -> None:
    """Measure what the store uses now, and record it on its row."""
    with store_session(data_store_id, "ro") as store:
        db_bytes, files_bytes = store.execute(_MEASURE).one()
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == data_store_id)
            .values(db_bytes=db_bytes, files_bytes=files_bytes, usage_measured_at=func.now())
        )
        db.commit()


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
        except Exception:
            log.exception("Couldn't measure data store %s", store_id)


def usage(data_store_id: uuid.UUID) -> Usage:
    """The store's usage, as last measured, against the configured caps."""
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
