"""Each data store's storage cap: the data its tables hold (`_DB_BYTES`) and its documents'
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

# The data the store's tables hold, which deleting rows brings down at once (the database's
# size, `pg_database_size`, keeps the space they took until their table is rewritten). For each
# table, `pgstattuple_approx` (the template's, which either login role may run) gives the bytes
# its live rows take, in the table's own pages and in its TOAST table (values over about 2 KB,
# kept apart). Its indexes count in proportion: each index's size times the share of its
# table's pages holding live rows rather than dead ones or free space, since an index doesn't
# shrink as rows go either. Pages vacuum marks all-visible are estimated from the free space it
# recorded, the rest read, so it's close rather than exact. The system catalogs don't count.
_DB_BYTES = text("""
SELECT coalesce(sum(
         heap.approx_tuple_len + coalesce(toast.approx_tuple_len, 0)
         + (SELECT coalesce(sum(pg_relation_size(i.indexrelid)), 0)
              FROM pg_index i WHERE i.indrelid = c.oid)
           * heap.approx_tuple_len
           / greatest(heap.approx_tuple_len + heap.dead_tuple_len + heap.approx_free_space, 1)
       ), 0)::bigint
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 CROSS JOIN LATERAL extensions.pgstattuple_approx(c.oid) heap
 -- A table without one has 0, and the function gives a row of nulls for a null.
 CROSS JOIN LATERAL extensions.pgstattuple_approx(nullif(c.reltoastrelid, 0)::regclass) toast
 WHERE c.relkind IN ('r', 'm') AND c.relpersistence <> 't'
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
""")
# The database's size, all its files: never less than the data in it.
_DB_SIZE = text("SELECT pg_database_size(current_database())")
# Whether this role may run `pgstattuple_approx` here (it's there, and granted to it).
_MAY_MEASURE = text("""
SELECT coalesce(has_function_privilege(to_regprocedure('extensions.pgstattuple_approx(regclass)'),
                                       'EXECUTE'), false)
""")
_FILES_BYTES = text("SELECT coalesce(sum(size_bytes), 0) FROM aios.documents")


def _db_bytes(store: Session, size: int | None = None) -> int:
    """The data the store holds (`_DB_BYTES`). A store made before the template had pgstattuple,
    which the roles command couldn't give it (`aios.db_roles`), is measured by its size (`size`,
    if it's been read already)."""
    if not store.scalar(_MAY_MEASURE):
        return store.scalar(_DB_SIZE) if size is None else size
    return store.scalar(_DB_BYTES)


def _db_full(store: Session, quota: int) -> int | None:
    """What the store holds, if it's at `quota` or past it; None if it isn't. The database's
    size is no less, and quick to read: under the quota, there's no need to measure."""
    size = store.scalar(_DB_SIZE)
    if size < quota:
        return None
    used = _db_bytes(store, size)
    return used if used >= quota else None


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
    if (db_bytes := _db_full(store, db_quota)) is not None:
        return (
            "The organisation's data store is full: its data takes up"
            f" {_of(db_bytes, db_quota)}, so documents can't be uploaded."
        )
    files_bytes = store.scalar(_FILES_BYTES)
    if files_bytes >= files_quota:
        return (
            "The organisation's data store is full: its documents use"
            f" {_of(files_bytes, files_quota)}. Delete some to make room."
        )
    if files_bytes + size > files_quota:
        return (
            f"There's no room for {filename}: the organisation's documents use"
            f" {_of(files_bytes, files_quota)}, and it's {format_bytes(size)}. Delete some to make"
            " room."
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
    if (db_bytes := _db_full(store, db_quota)) is not None:
        return (
            "The organisation's data store is full: its data takes up"
            f" {_of(db_bytes, db_quota)}, so rows can't be inserted or updated. Reading still"
            " works, and deleting rows makes room. Tell the user: the Data page shows its usage."
        )
    files_bytes = store.scalar(_FILES_BYTES)
    if files_bytes >= files_quota:
        return (
            "The organisation's data store is full: its documents use"
            f" {_of(files_bytes, files_quota)}, so rows can't be inserted or updated. Reading and"
            " deleting rows still work. Tell the user: deleting documents on the Data page makes"
            " room."
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
        db_bytes, files_bytes = _db_bytes(store), store.scalar(_FILES_BYTES)
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


def status_and_usage(data_store_id: uuid.UUID) -> tuple[DataStoreStatus, Usage]:
    """The store's status, and its usage as last recorded against the configured caps, from
    one read of its row."""
    with SessionLocal() as db:
        row = db.execute(
            select(
                DataStore.status,
                DataStore.db_bytes,
                DataStore.files_bytes,
                DataStore.usage_measured_at,
            ).where(DataStore.id == data_store_id)
        ).one()
    settings = get_settings()
    return row.status, Usage(
        db_bytes=row.db_bytes,
        db_quota_bytes=settings.data_store_db_quota_bytes,
        files_bytes=row.files_bytes,
        files_quota_bytes=settings.data_store_files_quota_bytes,
        measured_at=row.usage_measured_at,
    )
