"""In-app notifications: rows in `notifications`, each for one user, of a kind, with a reference
(jsonb) to what it's about. The SPA polls for the signed-in user's (`aios.api.notifications`)
and shows an unread badge.

Nothing here knows about any one kind. The first is a schema change request waiting for a
decision (`aios.datastores.schema_changes`), whose reference is the request's id and its
store's; others (a failed workflow run, a store over quota) can use the same rows later, and
email could deliver them too.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from aios.models import Notification

SCHEMA_CHANGE_REQUEST = "schema_change_request"


def notify(db: Session, user_ids: Iterable[int], kind: str, ref: dict[str, Any]) -> None:
    """Add a notification of `kind` about `ref` for each user, in the caller's transaction (so
    it commits, or doesn't, with what it's about)."""
    db.add_all(Notification(user_id=user_id, kind=kind, ref=ref) for user_id in user_ids)


def mark_read_about(db: Session, kind: str, ref: dict[str, Any]) -> None:
    """Mark every unread notification of `kind` whose reference contains `ref` read, for every
    user: what it's about no longer needs them. In the caller's transaction."""
    db.execute(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
    )


def mark_unread_about(db: Session, kind: str, ref: dict[str, Any], read_at: datetime) -> None:
    """Undo `mark_read_about` run in a transaction whose `now()` was `read_at`: the
    notifications it marked read (and only those) are unread again. In the caller's
    transaction."""
    db.execute(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at == read_at,
        )
        .values(read_at=None)
    )
