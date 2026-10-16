"""The user's settings (the Settings page). So far: the run history export
(`aios.datastores.history`)."""

import logging
from datetime import datetime

from fastapi import APIRouter, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select

from aios.auth import CurrentUser
from aios.datastores import data_store_for, history
from aios.db import DB
from aios.models import DataStore, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class SettingsIn(BaseModel):
    export_run_history: bool


class SettingsOut(BaseModel):
    # Copy agent runs into the data store's `history` schema.
    export_run_history: bool
    # The store has its `history` tables (they're made once the export is first switched on).
    history_ready: bool
    # When the export last copied runs into the store.
    history_exported_at: datetime | None


def settings_out(db: DB, user_id: int) -> SettingsOut:
    row = db.execute(
        select(
            User.export_run_history,
            DataStore.history_revision,
            DataStore.history_exported_at,
        )
        .outerjoin(DataStore, DataStore.owner_user_id == User.id)
        .where(User.id == user_id)
    ).one()
    return SettingsOut(
        export_run_history=row.export_run_history,
        history_ready=row.history_revision is not None,
        history_exported_at=row.history_exported_at,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings(user: CurrentUser, db: DB):
    return settings_out(db, user.id)


@router.put("/settings", response_model=SettingsOut)
def update_settings(body: SettingsIn, user: CurrentUser, db: DB):
    set_up = history.set_exporting(db, user.id, body.export_run_history)
    db.commit()
    if set_up is not None:
        try:
            history.enqueue_set_up(set_up)
        except Exception:
            # The setting is saved: the provisioning worker's retry sets the store up
            # (`provision.retry_unfinished`), and the export sweep then drains the backfill.
            log.exception("Couldn't queue setting up the history export of data store %s", set_up)
    return settings_out(db, user.id)


@router.delete("/settings/exported-history", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exported_history(user: CurrentUser) -> None:
    """Empty the data store's `history` tables. A 409 while the store isn't ready."""
    store_id = await run_in_threadpool(data_store_for, user.id)
    await run_in_threadpool(history.delete_exported, store_id)
