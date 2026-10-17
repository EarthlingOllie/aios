"""Schema change requests, for the people who decide them: the Data page's Changes section and
the approval card in an agent's chat (`aios.datastores.schema_changes`).

Only users who pass `can_approve` for a request's store see or decide it; to anyone else it
doesn't exist (404). Approving queues the change to be applied on the provisioning worker,
which then resumes the run that asked; rejecting resumes it straight away.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from aios.auth import CurrentUser
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    can_approve,
    data_store_for,
    dropped,
)
from aios.datastores.schema_changes import (
    SchemaChange,
    clear_notifications,
    confirmation,
    restore_notifications,
)
from aios.db import DB
from aios.models import AgentRun, SchemaChangeRequest, SchemaChangeStatus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class RequestRun(BaseModel):
    """The agent run that asked, for a link to it."""

    agent_id: int
    run_id: int
    title: str | None


class SchemaChangeOut(BaseModel):
    id: uuid.UUID
    data_store_id: uuid.UUID
    status: SchemaChangeStatus
    summary: str
    reason: str
    # Exactly what runs on approval.
    sql: str
    ops: list[dict[str, Any]]
    run: RequestRun | None
    # The tool call that asked, so a chat shows the request at that call.
    tool_call_id: str | None
    note: str | None
    error: str | None
    created_at: datetime
    decided_at: datetime | None
    applied_at: datetime | None
    # When the run that asked was given the outcome.
    resumed_at: datetime | None
    # Whether the signed-in user may approve or reject it (and it's still pending).
    can_decide: bool
    # What approving it takes typed, if it drops anything: the names of the tables it drops
    # from (`schema_changes.confirmation`).
    confirm_with: str | None


class Approval(BaseModel):
    # For a request that drops anything, its `confirm_with`, typed.
    confirm: str | None = Field(None, max_length=1000)


class RemovalOut(BaseModel):
    """What one of a request's drops would remove, as the store is now."""

    table: str
    # The column it drops; None for a whole table.
    column: str | None
    # The table's rows; None if it isn't there (any more).
    rows: int | None
    # A column's: how many rows have a value in it.
    values: int | None
    # A table's: its columns.
    columns: list[str] | None
    # Whether `rows` is Postgres's estimate: counting took too long.
    estimated: bool


class ImpactOut(BaseModel):
    removes: list[RemovalOut]


class Rejection(BaseModel):
    note: str | None = Field(None, max_length=2000)


def request_out(
    request: SchemaChangeRequest, run: AgentRun | None, user_id: int
) -> SchemaChangeOut:
    return SchemaChangeOut(
        id=request.id,
        data_store_id=request.data_store_id,
        status=request.status,
        summary=request.summary,
        reason=request.reason,
        sql=request.sql,
        ops=request.ops,
        run=RequestRun(agent_id=run.agent_id, run_id=run.id, title=run.title) if run else None,
        tool_call_id=request.tool_call_id,
        note=request.note,
        error=request.error,
        created_at=request.created_at,
        decided_at=request.decided_at,
        applied_at=request.applied_at,
        resumed_at=request.resumed_at,
        can_decide=request.status == SchemaChangeStatus.pending
        and can_approve(user_id, request.data_store),
        confirm_with=confirmation_for(request),
    )


def confirmation_for(request: SchemaChangeRequest) -> str | None:
    return confirmation(SchemaChange.model_validate({"ops": request.ops}))


def _query():
    return select(SchemaChangeRequest, AgentRun).outerjoin(
        AgentRun, AgentRun.id == SchemaChangeRequest.requested_by_run
    )


def run_requests(db: Session, run_id: int, user_id: int) -> list[SchemaChangeOut]:
    """The requests a run made, oldest first (for its chat)."""
    rows = db.execute(
        _query()
        .where(SchemaChangeRequest.requested_by_run == run_id)
        .order_by(SchemaChangeRequest.created_at)
    ).all()
    return [request_out(request, run, user_id) for request, run in rows]


def waiting_on(db: Session, run_id: int) -> bool:
    """Whether the run waits on a request: not yet resumed with its outcome."""
    return (
        db.scalar(
            select(func.count())
            .select_from(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
            )
        )
        > 0
    )


@router.get("/schema-changes", response_model=list[SchemaChangeOut])
def list_requests(user: CurrentUser, db: DB):
    """The user's store's requests: pending first (oldest first), then the decided ones, most
    recently decided first."""
    store_id = data_store_for(user.id, db)
    db.commit()
    pending = SchemaChangeRequest.status == SchemaChangeStatus.pending
    rows = db.execute(
        _query()
        .where(SchemaChangeRequest.data_store_id == store_id)
        .order_by(
            case((pending, 0), else_=1),
            case((pending, SchemaChangeRequest.created_at)),
            func.coalesce(SchemaChangeRequest.decided_at, SchemaChangeRequest.created_at).desc(),
        )
    ).all()
    return [
        request_out(request, run, user.id)
        for request, run in rows
        if can_approve(user.id, request.data_store)
    ]


def _get(db: Session, user_id: int, request_id: uuid.UUID, *, lock: bool = False):
    query = select(SchemaChangeRequest).where(SchemaChangeRequest.id == request_id)
    request = db.scalar(query.with_for_update() if lock else query)
    if request is None or not can_approve(user_id, request.data_store):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schema change request not found")
    return request


def _out(db: Session, request: SchemaChangeRequest, user_id: int) -> SchemaChangeOut:
    run = db.get(AgentRun, request.requested_by_run) if request.requested_by_run else None
    return request_out(request, run, user_id)


@router.get("/schema-changes/{request_id}", response_model=SchemaChangeOut)
def get_request(request_id: uuid.UUID, user: CurrentUser, db: DB):
    return _out(db, _get(db, user.id, request_id), user.id)


def _decide(db: Session, user_id: int, request_id: uuid.UUID, **values) -> SchemaChangeRequest:
    # Locked, so two decisions at once can't both pass the pending check.
    request = _get(db, user_id, request_id, lock=True)
    if request.status != SchemaChangeStatus.pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"This request has already been {request.status.value}"
        )
    for key, value in values.items():
        setattr(request, key, value)
    request.decided_by_user_id = user_id
    request.decided_at = func.now()
    # With the decision, so they're cleared if and only if it's made.
    clear_notifications(db, request.id)
    db.commit()
    db.refresh(request)
    return request


def _queue(db: Session, request: SchemaChangeRequest, queue: Callable[[], None]) -> None:
    """Queue what follows a decision; if it can't be (the broker is down), undo the decision,
    which nothing else has seen through, so it's not left decided with nothing to act on it, and
    its notifications are unread again."""
    try:
        queue()
    except Exception as exc:
        log.exception("Couldn't queue what follows deciding schema change request %s", request.id)
        restore_notifications(db, request.id, request.decided_at)
        db.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.id == request.id,
                SchemaChangeRequest.status == request.status,
            )
            .values(
                status=SchemaChangeStatus.pending,
                decided_by_user_id=None,
                decided_at=None,
                note=None,
            )
        )
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The decision couldn't be passed on just now, so it wasn't made. Try again.",
        ) from exc


@router.get("/schema-changes/{request_id}/impact", response_model=ImpactOut)
def get_impact(request_id: uuid.UUID, user: CurrentUser, db: DB):
    """What the request's drops would remove from the store as it is now (row counts, the
    columns affected), for the owner deciding it; nothing for one that drops nothing."""
    request = _get(db, user.id, request_id)
    try:
        removes = dropped.impact(request.data_store_id, request.ops)
    except (DataStoreBehind, DataStoreBusy, DataStoreNotFound, DataStoreNotReady) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Your data store can't be read just now."
        ) from exc
    return ImpactOut(removes=[RemovalOut(**vars(r)) for r in removes])


@router.post("/schema-changes/{request_id}/approve", response_model=SchemaChangeOut)
def approve(request_id: uuid.UUID, user: CurrentUser, db: DB, body: Approval | None = None):
    from aios.worker import apply_schema_change_task

    # Typed by the owner, so a drop is never approved by a stray click (its ops never change).
    required = confirmation_for(_get(db, user.id, request_id))
    typed = ((body.confirm if body else None) or "").strip()
    if required is not None and typed != required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"This request drops data: Type `{required}` to confirm.",
        )
    request = _decide(db, user.id, request_id, status=SchemaChangeStatus.approved)
    _queue(db, request, lambda: apply_schema_change_task.delay(str(request.id)))
    return _out(db, request, user.id)


@router.post("/schema-changes/{request_id}/reject", response_model=SchemaChangeOut)
def reject(request_id: uuid.UUID, body: Rejection, user: CurrentUser, db: DB):
    from aios.worker import enqueue_resume

    note = (body.note or "").strip() or None
    request = _decide(db, user.id, request_id, status=SchemaChangeStatus.rejected, note=note)
    _queue(db, request, lambda: enqueue_resume(request.id))
    return _out(db, request, user.id)
