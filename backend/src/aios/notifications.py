"""In-app notifications: rows in `notifications`, each for one user, of a kind, with a reference
(jsonb) to what it's about. The SPA polls for the signed-in user's (`aios.api.notifications`)
and shows an unread badge.

Nothing here knows about any one kind. The first is a schema change request waiting for a
decision (`aios.datastores.schema_changes`), whose reference is the request's id and its
store's; others (a failed workflow run, a store over quota) can use the same rows later, and
email could deliver them too.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios.db import SessionLocal
from aios.models import DataStore, Notification

SCHEMA_CHANGE_REQUEST = "schema_change_request"


def notify(
    db: Session, user_ids: Iterable[int], organisation_id: int, kind: str, ref: dict[str, Any]
) -> None:
    """Add a notification of `kind` about `ref`, from the organisation, for each user, in the
    caller's transaction (so it commits, or doesn't, with what it's about)."""
    db.add_all(
        Notification(user_id=user_id, organisation_id=organisation_id, kind=kind, ref=ref)
        for user_id in user_ids
    )


def mark_read_about(
    db: Session, kind: str, ref: dict[str, Any], at: datetime | None = None
) -> None:
    """Mark every unread notification of `kind` whose reference contains `ref` read (as of
    `at`, else now), for every user: what it's about no longer needs them. In the caller's
    transaction."""
    db.execute(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now() if at is None else at)
    )


def mark_unread_about(db: Session, kind: str, ref: dict[str, Any], read_at: datetime) -> None:
    """Undo `mark_read_about` that marked them read as of `read_at`: the notifications it
    marked read (and only those) are unread again. In the caller's transaction."""
    db.execute(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at == read_at,
        )
        .values(read_at=None)
    )


def notify_from_store(
    data_store_id: uuid.UUID, user_ids: Iterable[int], kind: str, ref: dict[str, Any]
) -> None:
    """`notify`, from the organisation whose store this is, in a platform transaction of its own
    (committed): for what a store-side job finds (`aios.verdicts`). Nothing if the store has no
    organisation any more."""
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        if organisation_id is None:
            return
        notify(db, user_ids, organisation_id, kind, ref)
        db.commit()
