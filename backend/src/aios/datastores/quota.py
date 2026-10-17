"""Each data store's storage cap: its database's size (`pg_database_size`) and its documents'
files' bytes, against `data_store_db_quota_bytes` and `data_store_files_quota_bytes`.

`measure_all`, on a schedule (`aios.worker`), records what each ready store uses on its
`data_stores` row; the checks read that record, so a store is over its cap from the first
measurement past it until one back under it (its owner deleted something). Over either cap,
uploads and `write_data`'s inserts and updates are refused; reads, searches and deletes carry on.
"""

import logging
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
