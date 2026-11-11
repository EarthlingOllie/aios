"""Closing requests out: their notifications, cancelling those nothing waits on any more
(`cancel`), and expiring those left undecided (`expire`)."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from aios import notifications
from aios.config import get_settings
from aios.datastores.connections import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_session,
)
from aios.datastores.models import WorkflowRunStep
from aios.db import SessionLocal
from aios.models import SchemaChangeRequest, SchemaChangeStatus


def _notification_ref(request: SchemaChangeRequest) -> dict[str, str]:
    return {"request_id": str(request.id), "data_store_id": str(request.data_store_id)}


def summaries(db: Session, request_ids: set[str]) -> dict[str, str]:
    """The summaries of those of these requests (by id, as text) that still exist, by id: what
    their notifications say."""
    ids = []
    for request_id in request_ids:
        try:
            ids.append(uuid.UUID(request_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    rows = db.execute(
        select(SchemaChangeRequest.id, SchemaChangeRequest.summary).where(
            SchemaChangeRequest.id.in_(ids)
        )
    )
    return {str(request_id): summary for request_id, summary in rows}


def not_given() -> list[ColumnElement[bool]]:
    """A request the run that asked still waits on: not yet given its outcome, nor cancelled
    (whose run has been told, or is gone)."""
    return [
        SchemaChangeRequest.resumed_at.is_(None),
        SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
    ]


def clear_notifications(db: Session, request_id: uuid.UUID) -> None:
    """Mark the request's notifications read, for everyone it notified: it no longer waits for
    a decision (it's decided, cancelled or expired). In the caller's transaction."""
    notifications.mark_read_about(
        db, notifications.SCHEMA_CHANGE_REQUEST, {"request_id": str(request_id)}
    )


def _close(
    db: Session, status: SchemaChangeStatus, *where: ColumnElement[bool], **values: Any
) -> list[uuid.UUID]:
    """Take the pending requests matching `where` out of `pending` with `status` (and
    `values`), clearing their notifications; returns their ids. Only one still pending is
    closed, in one statement: one being decided meanwhile holds its row
    (`aios.api.schema_changes._decide`), and this waits for it, then leaves it. In the caller's
    transaction."""
    closed = db.scalars(
        update(SchemaChangeRequest)
        .where(*where, SchemaChangeRequest.status == SchemaChangeStatus.pending)
        .values(status=status, decided_at=func.now(), **values)
        .returning(SchemaChangeRequest.id)
    ).all()
    # One by one, each by the jsonb index; there are seldom more than a few at once.
    for request_id in closed:
        clear_notifications(db, request_id)
    return list(closed)


def cancel(db: Session, *where: ColumnElement[bool]) -> list[uuid.UUID]:
    """Cancel the pending requests matching `where`, clearing their notifications; returns
    their ids. Nothing will resume the runs that asked (they're gone, or have been told the
    call didn't finish), so they count as given. In the caller's transaction."""
    return _close(db, SchemaChangeStatus.cancelled, *where, resumed_at=func.now())


def expire() -> list[uuid.UUID]:
    """Expire the requests still pending after `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`, clearing
    their notifications; returns their ids. Expiring counts as a rejection: the runs that asked
    are then resumed with it (`aios.worker.expire_schema_change_requests_task`)."""
    age = timedelta(days=get_settings().schema_change_request_expiry_days)
    with SessionLocal() as db:
        expired = _close(
            db, SchemaChangeStatus.expired, SchemaChangeRequest.created_at < func.now() - age
        )
        db.commit()
    return expired


def expired_not_resumed() -> list[uuid.UUID]:
    """Expired requests whose runs haven't been given the outcome yet: those to resume. Each
    run of the scheduled task queues them all again, so one whose queuing failed isn't lost
    (resuming twice is safe).

    One nothing can resume any more (its agent run is gone, and no workflow step waits on it)
    counts as given instead, so it isn't looked at again."""
    not_given = [
        SchemaChangeRequest.status == SchemaChangeStatus.expired,
        SchemaChangeRequest.resumed_at.is_(None),
    ]
    with SessionLocal() as db:
        runless = db.execute(
            select(SchemaChangeRequest.id, SchemaChangeRequest.data_store_id).where(
                *not_given, SchemaChangeRequest.requested_by_run.is_(None)
            )
        ).all()
    by_store: dict[uuid.UUID, set[uuid.UUID]] = {}
    for request_id, store_id in runless:
        by_store.setdefault(store_id, set()).add(request_id)
    # Those no workflow step waits on, in their store (left be while it can't be read).
    gone: set[uuid.UUID] = set()
    for store_id, ids in by_store.items():
        try:
            with store_session(store_id, "ro") as store:
                waited = set(
                    store.scalars(
                        select(WorkflowRunStep.waiting_on_request_id).where(
                            WorkflowRunStep.waiting_on_request_id.in_(ids)
                        )
                    )
                )
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
            continue
        gone |= ids - waited
    with SessionLocal() as db:
        if gone:
            db.execute(
                update(SchemaChangeRequest)
                .where(*not_given, SchemaChangeRequest.id.in_(gone))
                .values(resumed_at=func.now())
            )
        ids = list(db.scalars(select(SchemaChangeRequest.id).where(*not_given)))
        db.commit()
    return ids


def restore_notifications(db: Session, request_id: uuid.UUID, cleared_at: datetime) -> None:
    """Undo `clear_notifications` for a decision that was undone (the request waits again):
    those it marked read, in the transaction that ran at `cleared_at`, are unread again; any a
    user marked read themselves stay read. In the caller's transaction."""
    notifications.mark_unread_about(
        db, notifications.SCHEMA_CHANGE_REQUEST, {"request_id": str(request_id)}, cleared_at
    )
