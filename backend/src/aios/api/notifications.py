"""The signed-in person's notifications (`aios.notifications`), from every organisation they're
in, under the person (`/api/me/notifications`), not any organisation: list them, mark one read,
mark all read. Each person only ever sees, or marks, their own: anyone else's is a 404.

What each says is read from its organisation's store as it's listed; one whose reference no
longer exists there, or from an organisation the person has left, is dropped from the list and
marked read. The bell hears of changes on the person's stream (`aios.api.streams`)."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios import notifications
from aios.auth import CurrentUser, same_origin
from aios.db import DB
from aios.models import Notification, Organisation

# Its POSTs refuse cross-site requests.
router = APIRouter(prefix="/api/me/notifications", dependencies=[Depends(same_origin)])

# The most listed at once: all the unread ones a person is likely to have, and the latest read.
LIST_LIMIT = 50


class NotificationOrganisationOut(BaseModel):
    id: int
    # What links in the browser name it by: opening the notification goes to `/o/<slug>/...`.
    slug: str
    # What the bell labels it with.
    name: str


class NotificationOut(BaseModel):
    id: uuid.UUID
    # The organisation it's from.
    organisation: NotificationOrganisationOut
    # `mention`, `reply`, `run_failed`, `approval_needed`, `released_document_changed`,
    # `organisation_deletion_scheduled` or `organisation_deletion_cancelled`.
    kind: str
    # What it's about in its organisation, never content: for `mention`, `reply` and a chat's
    # `run_failed`, `chat_id` and `message_id`; for a run's `run_failed`, `agent_id` or
    # `workflow_id`, and `run_id`; for `approval_needed`, `approval_id`; for
    # `released_document_changed`, `document_id`; for an organisation's deletion, nothing.
    ref: dict[str, Any]
    # What it says, in a line, and more if there's more (an excerpt, why a run failed), read
    # from its organisation's store now. Null while that can't be read.
    title: str | None
    detail: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationList(BaseModel):
    # How many of the person's are unread, all told (not only those listed).
    unread: int
    # Unread first, then read; newest first within each.
    notifications: list[NotificationOut]


def _organisations(db: Session, ids: set[int]) -> dict[int, Organisation]:
    return {o.id: o for o in db.scalars(select(Organisation).where(Organisation.id.in_(ids)))}


def _out(
    n: Notification, org: Organisation, said: notifications.Description | None
) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        organisation=NotificationOrganisationOut(id=org.id, slug=org.slug, name=org.name),
        kind=n.kind,
        ref=n.ref,
        title=said.title if said else None,
        detail=said.detail if said else None,
        created_at=n.created_at,
        read_at=n.read_at,
    )


def _unread(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )


def _listed(db: Session, user_id: int, rows: Sequence[Notification]) -> list[NotificationOut]:
    """The rows as listed: those whose references are gone dropped, and marked read
    (committed)."""
    said = notifications.describe(db, rows)
    gone = [n.id for n in rows if n.id in said and said[n.id] is None]
    if gone:
        notifications.mark_read(db, user_id, gone)
        db.commit()
    orgs = _organisations(db, {n.organisation_id for n in rows})
    return [
        _out(n, orgs[n.organisation_id], said.get(n.id))
        for n in rows
        if n.id not in gone and n.organisation_id in orgs
    ]


@router.get("", response_model=NotificationList)
def list_notifications(user: CurrentUser, db: DB):
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.read_at.is_not(None), Notification.created_at.desc())
        .limit(LIST_LIMIT)
    ).all()
    listed = _listed(db, user.id, rows)
    return NotificationList(unread=_unread(db, user.id), notifications=listed)


@router.post("/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(user: CurrentUser, db: DB) -> None:
    """Mark every one of the person's notifications read."""
    notifications.mark_all_read(db, user.id)
    db.commit()


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: uuid.UUID, user: CurrentUser, db: DB):
    """Mark one of the person's notifications read. Idempotent: one already read keeps when it
    was first read."""
    notifications.mark_read(db, user.id, [notification_id])
    db.commit()
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id
        )
    )
    if notification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    said = notifications.describe(db, [notification]).get(notification.id)
    [org] = _organisations(db, {notification.organisation_id}).values()
    return _out(notification, org, said)
