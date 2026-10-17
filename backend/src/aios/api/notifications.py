"""The signed-in user's notifications (`aios.notifications`), which the SPA polls for its unread
badge. Each user only ever sees, or marks, their own: anyone else's is a 404."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from aios.auth import CurrentUser
from aios.db import DB
from aios.models import Notification

router = APIRouter(prefix="/api")

# The most listed at once: all the unread ones a user is likely to have, and the latest read.
LIST_LIMIT = 50


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    # What it's about: for `schema_change_request`, `request_id` and `data_store_id`.
    ref: dict[str, Any]
    created_at: datetime
    read_at: datetime | None


class NotificationList(BaseModel):
    # How many of the user's are unread, all told (not only those listed).
    unread: int
    # Unread first, then read; newest first within each.
    notifications: list[NotificationOut]


def _out(notification: Notification) -> NotificationOut:
    return NotificationOut.model_validate(notification, from_attributes=True)


@router.get("/notifications", response_model=NotificationList)
def list_notifications(user: CurrentUser, db: DB):
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.read_at.is_not(None), Notification.created_at.desc())
        .limit(LIST_LIMIT)
    ).all()
    unread = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    return NotificationList(unread=unread, notifications=[_out(n) for n in rows])
