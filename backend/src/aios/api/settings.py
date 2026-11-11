"""The organisation's settings, on the Organisation page: so far, the run history export
(`aios.datastores.history`), kept on its data store. Any member reads them; admins change
them (`aios.permissions`).

The user's own Settings page has nothing of theirs to change yet, so nothing here is the
user's."""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select

from aios import audit
from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import data_store_for, history
from aios.db import DB
from aios.models import DataStore
from aios.permissions import Action, allow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class SettingsIn(BaseModel):
    # Either, or both; one left out stays as it is.
    export_run_history: bool | None = None
    embed_run_history: bool | None = None


class SettingsOut(BaseModel):
    # Copy agent and workflow runs into the data store's `history` schema.
    export_run_history: bool
    # The store has its `history` tables (they're made once the export is first switched on).
    history_ready: bool
    # When the export last caught up (copied in whatever runs were pending).
    history_exported_at: datetime | None
    # Embed the exported messages, so agents can search past runs by meaning. Only while the
    # export is on.
    embed_run_history: bool
    # When embedding last caught up (embedded every exported message waiting).
    history_embedded_at: datetime | None
    # Why it last failed, if it hasn't caught up since (it's tried again in a few minutes).
    history_embed_error: str | None


def settings_out(db: DB, organisation_id: int) -> SettingsOut:
    store = db.scalar(select(DataStore).where(DataStore.owner_organisation_id == organisation_id))
    if store is None:
        # Not made yet (signing up makes it): nothing switched on, nothing exported.
        return SettingsOut(
            export_run_history=False,
            history_ready=False,
            history_exported_at=None,
            embed_run_history=False,
            history_embedded_at=None,
            history_embed_error=None,
        )
    return SettingsOut(
        export_run_history=store.export_run_history,
        history_ready=store.history_revision is not None,
        history_exported_at=store.history_exported_at,
        embed_run_history=store.embed_run_history,
        history_embedded_at=store.history_embedded_at,
        history_embed_error=store.history_embed_error,
    )


@router.get("/organisation/settings", response_model=SettingsOut, dependencies=[allow(Action.read)])
def get_settings(org: CurrentOrganisation, db: DB):
    return settings_out(db, org.id)


@router.put(
    "/organisation/settings",
    response_model=SettingsOut,
    dependencies=[allow(Action.run_organisation)],
)
def update_settings(body: SettingsIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    before = settings_out(db, org.id)
    set_up = None
    if body.export_run_history is not None:
        set_up = history.set_exporting(db, org.id, body.export_run_history)
    if body.embed_run_history is not None:
        try:
            history.set_embedding(db, org.id, body.embed_run_history)
        except history.ExportOff:
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Switch the run history export on first."
            ) from None
    db.flush()
    after = settings_out(db, org.id)
    changes = {
        name: {"from": getattr(before, name), "to": getattr(after, name)}
        for name in body.model_fields_set
        if getattr(body, name) is not None and getattr(before, name) != getattr(after, name)
    }
    if changes:
        audit.enqueue(
            db, org.id, audit.person(user.id), "settings.changed", "organisation", org.id, changes
        )
    db.commit()
    if set_up is not None:
        try:
            history.enqueue_set_up(set_up)
        except Exception:
            # The setting is saved: the provisioning worker's retry sets the store up
            # (`provision.retry_unfinished`), and the export sweep then drains the backfill.
            log.exception("Couldn't queue setting up the history export of data store %s", set_up)
    return settings_out(db, org.id)


@router.delete(
    "/organisation/settings/exported-history",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.run_organisation)],
)
async def delete_exported_history(org: CurrentOrganisation) -> None:
    """Empty the data store's `history` tables. A 409 while the store isn't ready."""
    store_id = await run_in_threadpool(data_store_for, org.id)
    await run_in_threadpool(history.delete_exported, store_id)
