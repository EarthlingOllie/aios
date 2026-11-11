"""Jev's flagged documents, for admins (`aios.verdicts`): the list, with Jev's reason, and
releasing a false alarm, which makes it searchable (still wrapped as untrusted) and is recorded
in the audit log. And Jev's health, for the notice everyone sees while it's down."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from aios import jev_health, verdicts
from aios.api.provenance import UserOut, people
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.db import DB, DbId
from aios.jev import Verdict
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")


class FlaggedOut(BaseModel):
    id: int
    filename: str
    # Jev's reason.
    reason: str | None
    checked_at: datetime | None
    uploaded_by: UserOut | None
    created_at: datetime


class ReleasedOut(BaseModel):
    id: int
    verdict: Verdict


class JevStatusOut(BaseModel):
    # Whether Jev answered its last health check; null if it hasn't been checked lately.
    up: bool | None


@router.get("/flagged", response_model=list[FlaggedOut], dependencies=[allow(Action.release)])
def list_flagged(org: CurrentOrganisation, db: DB, store: CurrentStore):
    """The organisation's flagged documents, with Jev's reason, the most recently checked
    first."""
    found = verdicts.flagged(store)
    by = people(db, org.id, [f.uploaded_by for f in found if f.uploaded_by is not None])
    return [
        FlaggedOut(
            id=f.id,
            filename=f.filename,
            reason=f.reason,
            checked_at=f.checked_at,
            uploaded_by=by.get(f.uploaded_by) if f.uploaded_by is not None else None,
            created_at=f.created_at,
        )
        for f in found
    ]


@router.post(
    "/flagged/{document_id}/release",
    response_model=ReleasedOut,
    dependencies=[allow(Action.release)],
)
def release_flagged(document_id: DbId, user: CurrentUser, store: CurrentStore):
    """Release a flagged document: it's found by search and reaches agents' context from now on,
    still wrapped as untrusted. 409 if it isn't flagged."""
    try:
        doc = verdicts.release(store, document_id, user.id)
    except verdicts.NotFlagged:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a flagged document can be released."
        ) from None
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return ReleasedOut(id=doc.id, verdict=doc.verdict)


@router.get("/jev-status", response_model=JevStatusOut, dependencies=[allow(Action.read)])
def jev_status():
    """Jev's health as last checked, so the app can say why documents wait and tools fail."""
    return JevStatusOut(up=jev_health.status().up)
