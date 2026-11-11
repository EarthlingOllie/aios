"""Closing requests out: their notifications, cancelling those nothing waits on any more
(`cancel`), and expiring those left undecided (`expire`).

Requests are in their store, the source of truth, and notifications on the platform, which
name each request by its id and store: a request is closed in its store first, and its
notifications are cleared once that has committed (`clear_notifications_of`)."""

import logging
import uuid
from collections.abc import Iterable
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
    ready_stores,
    store_session,
)
from aios.datastores.models import SchemaChangeRequest, WorkflowRunStep
from aios.db import SessionLocal
from aios.models import SchemaChangeStatus

log = logging.getLogger(__package__)

# A request, named outside its store: the store, and the request's id.
StoredRequest = tuple[uuid.UUID, uuid.UUID]


def _notification_ref(data_store_id: uuid.UUID, request_id: uuid.UUID) -> dict[str, str]:
    return {"request_id": str(request_id), "data_store_id": str(data_store_id)}


def summaries(refs: Iterable[tuple[Any, Any]]) -> dict[str, str]:
    """The summaries of those of these requests (each its store's id and its own, as
    notifications name them, as text) that still exist, by request id: what their
    notifications say. A store that can't be read leaves its requests without one."""
    by_store: dict[uuid.UUID, set[uuid.UUID]] = {}
    for store_id, request_id in refs:
        try:
            by_store.setdefault(uuid.UUID(store_id), set()).add(uuid.UUID(request_id))
        except (TypeError, ValueError, AttributeError):
            continue
    found: dict[str, str] = {}
    for store_id, ids in by_store.items():
        try:
            with store_session(store_id, "ro") as store:
                rows = store.execute(
                    select(SchemaChangeRequest.id, SchemaChangeRequest.summary).where(
                        SchemaChangeRequest.id.in_(ids)
                    )
                )
                found.update((str(request_id), summary) for request_id, summary in rows)
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
            continue
    return found


def not_given() -> list[ColumnElement[bool]]:
    """A request the run that asked still waits on: not yet given its outcome, nor cancelled
    (whose run has been told, or is gone)."""
    return [
        SchemaChangeRequest.resumed_at.is_(None),
        SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
    ]


def clear_notifications(
    db: Session, request_ids: Iterable[uuid.UUID], at: datetime | None = None
) -> None:
    """Mark the requests' notifications read (as of `at`, else now), for everyone they
    notified: the requests no longer wait for a decision (they're decided, cancelled or
    expired). In the caller's transaction."""
    # One by one, each by the jsonb index; there are seldom more than a few at once.
    for request_id in request_ids:
        notifications.mark_read_about(
            db, notifications.SCHEMA_CHANGE_REQUEST, {"request_id": str(request_id)}, at
        )


def clear_notifications_of(request_ids: Iterable[uuid.UUID]) -> None:
    """`clear_notifications` for requests their store has just closed, committed. One that
    fails is logged: the requests are closed either way, and their notifications only say
    less than they might."""
    request_ids = list(request_ids)
    if not request_ids:
        return
    try:
        with SessionLocal() as db:
            clear_notifications(db, request_ids)
            db.commit()
    except Exception:
        log.exception("couldn't clear the notifications of requests %s", request_ids)


def _close(
    store: Session, status: SchemaChangeStatus, *where: ColumnElement[bool], **values: Any
) -> list[uuid.UUID]:
    """Take the store's pending requests matching `where` out of `pending` with `status` (and
    `values`); returns their ids, whose notifications to clear once this commits
    (`clear_notifications_of`). Only one still pending is closed, in one statement: one being
    decided meanwhile holds its row (`aios.api.schema_changes._decide`), and this waits for it,
    then leaves it. In the caller's transaction."""
    return list(
        store.scalars(
            update(SchemaChangeRequest)
            .where(*where, SchemaChangeRequest.status == SchemaChangeStatus.pending)
            .values(status=status, decided_at=func.now(), **values)
            .returning(SchemaChangeRequest.id)
        ).all()
    )


def cancel(store: Session, *where: ColumnElement[bool]) -> list[uuid.UUID]:
    """Cancel the store's pending requests matching `where`; returns their ids, whose
    notifications to clear once this commits (`clear_notifications_of`). Nothing will resume the
    runs that asked (they're gone, or have been told the call didn't finish), so they count as
    given. In the caller's transaction."""
    return _close(store, SchemaChangeStatus.cancelled, *where, resumed_at=func.now())


def expire() -> list[StoredRequest]:
    """Expire the requests, in every ready store, still pending after
    `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`, clearing their notifications; returns them. Expiring
    counts as a rejection: the runs that asked are then resumed with it
    (`aios.worker.expire_schema_change_requests_task`). A store that can't be reached is left
    for the next run."""
    age = timedelta(days=get_settings().schema_change_request_expiry_days)
    expired: list[StoredRequest] = []
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                ids = _close(
                    store,
                    SchemaChangeStatus.expired,
                    SchemaChangeRequest.created_at < func.now() - age,
                )
                store.commit()
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy) as exc:
            log.warning("couldn't expire the requests of data store %s: %s", store_id, exc)
            continue
        clear_notifications_of(ids)
        expired.extend((store_id, request_id) for request_id in ids)
    return expired


def expired_not_resumed() -> list[StoredRequest]:
    """Expired requests, in every ready store, whose runs haven't been given the outcome yet:
    those to resume. Each run of the scheduled task queues them all again, so one whose queuing
    failed isn't lost (resuming twice is safe).

    One nothing can resume any more (its agent run is gone, and no workflow step waits on it)
    counts as given instead, so it isn't looked at again."""
    not_resumed = [
        SchemaChangeRequest.status == SchemaChangeStatus.expired,
        SchemaChangeRequest.resumed_at.is_(None),
    ]
    found: list[StoredRequest] = []
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                waited = select(WorkflowRunStep.id).where(
                    WorkflowRunStep.waiting_on_request_id == SchemaChangeRequest.id
                )
                store.execute(
                    update(SchemaChangeRequest)
                    .where(
                        *not_resumed,
                        SchemaChangeRequest.requested_by_run.is_(None),
                        ~waited.exists(),
                    )
                    .values(resumed_at=func.now())
                )
                ids = list(store.scalars(select(SchemaChangeRequest.id).where(*not_resumed)))
                store.commit()
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
            continue
        found.extend((store_id, request_id) for request_id in ids)
    return found


def restore_notifications(db: Session, request_id: uuid.UUID, cleared_at: datetime) -> None:
    """Undo `clear_notifications` for a decision that was undone (the request waits again):
    those it marked read as of `cleared_at` are unread again; any a user marked read themselves
    stay read. In the caller's transaction."""
    notifications.mark_unread_about(
        db, notifications.SCHEMA_CHANGE_REQUEST, {"request_id": str(request_id)}, cleared_at
    )
