"""The Data page's Dropped section: the tables and columns approved drops took away, until
they're purged, and restoring them (`aios.datastores.dropped`).

Only users who pass `can_approve` for the store (every member of its organisation) see or
restore them. Restoring is queued to the provisioning worker (only it acts as the store's
owner role); one refused there says why on the item.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    can_approve,
    data_store_for,
    dropped,
)
from aios.db import DB
from aios.models import DataStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class DroppedOut(BaseModel):
    id: int
    kind: Literal["table", "column"]
    # A table's name, or the table a column is in.
    table: str
    column: str | None
    # What it was renamed to while it's dropped.
    dropped_name: str
    dropped_at: datetime
    # When it's purged for good.
    purge_at: datetime
    # Why it can't be restored now, if it can't (its name has been taken since, say).
    blocked: str | None
    # Why the last restore asked for was refused, if it was.
    restore_error: str | None


def _out(item: dropped.Dropped, blocked: str | None) -> DroppedOut:
    return DroppedOut(
        id=item.id,
        kind=item.kind,
        table=item.table,
        column=item.column,
        dropped_name=item.dropped_name,
        dropped_at=item.dropped_at,
        purge_at=dropped.purge_at(item),
        blocked=blocked,
        restore_error=item.restore_error,
    )


def _store(user_id: int, organisation_id: int, db: DB):
    """The organisation's store, if the user may restore what's dropped in it."""
    store_id = data_store_for(organisation_id, db)
    db.commit()
    if not can_approve(db, user_id, db.get_one(DataStore, store_id)):
        return None
    return store_id


def _read(store_id, fn, *args):
    """`fn(store_id, *args)` on the store; None for a store that isn't set up (nothing's
    dropped in it), and 503 for one that can't be read just now (busy, or being upgraded),
    which is worth trying again rather than taken to mean nothing's there."""
    try:
        return fn(store_id, *args) if store_id else None
    except (DataStoreBehind, DataStoreBusy) as exc:  # Behind before NotReady: it's one
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Your data store can't be read just now."
        ) from exc
    except (DataStoreNotFound, DataStoreNotReady):
        return None


@router.get("/dropped", response_model=list[DroppedOut])
def list_dropped(user: CurrentUser, org: CurrentOrganisation, db: DB):
    """What's dropped in the organisation's store, newest first."""
    listed = _read(_store(user.id, org.id, db), dropped.listing) or []
    return [_out(item, why) for item, why in listed]


@router.post(
    "/dropped/{dropped_id}/restore",
    response_model=DroppedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def restore(dropped_id: int, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Queue restoring it, unless it can't be restored now (409, saying why)."""
    from aios.worker import restore_dropped_task

    store_id = _store(user.id, org.id, db)
    found = _read(store_id, dropped.find, dropped_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing dropped by that id")
    item, why = found
    if why is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, why)
    try:
        restore_dropped_task.delay(str(store_id), dropped_id)
    except Exception as exc:
        log.exception("Couldn't queue restoring %s in data store %s", dropped_id, store_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "It couldn't be restored just now. Try again."
        ) from exc
    return _out(item, None)
