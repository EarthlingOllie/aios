"""Schema change requests, for the people who decide them: the Data page's Changes section and
the approval card in an agent's chat (`aios.datastores.schema_changes`).

Requests are in the organisation's store. Only users who pass `can_approve` for a request's
store (every member of its organisation) see it; to anyone else it doesn't exist (404).
Deciding one takes a role that may (editors and admins, `aios.permissions`), and at least the
role its operations need (`aios.approval_rules`): each request says which (`needs_role`), and
whether the user may (`can_decide`). Each decision is audited (`aios.audit`).
Approving queues the change to be applied on the urgent worker, which then resumes the
run that asked; rejecting resumes it straight away.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, case, func, select, update
from sqlalchemy.orm import Session

from aios import approval_rules, audit, organisations
from aios.auth import CurrentStore, CurrentUser
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    can_approve,
    dropped,
    store_id_of,
)
from aios.datastores.models import AgentRun, SchemaChangeRequest
from aios.datastores.schema_changes import (
    SchemaChange,
    clear_notifications,
    confirmation,
    not_given,
    restore_notifications,
)
from aios.db import DB
from aios.models import (
    DataStore,
    Role,
    SchemaChangeStatus,
)
from aios.permissions import Action, allow, can

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
    # The least role that may decide it: the highest its operations need (`aios.approval_rules`).
    needs_role: Role
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
    data_store_id: uuid.UUID,
    request: SchemaChangeRequest,
    run: RequestRun | None,
    role: Role | None,
    rules: dict[str, Role],
) -> SchemaChangeOut:
    """The request, for a member with `role` (None for one who may decide none of its store's
    requests), under the store's approval `rules`: they may decide it only while it's pending,
    and if their role is at least what it needs."""
    needs = approval_rules.needed(rules, request.ops)
    return SchemaChangeOut(
        id=request.id,
        data_store_id=data_store_id,
        status=request.status,
        summary=request.summary,
        reason=request.reason,
        sql=request.sql,
        ops=request.ops,
        run=run,
        tool_call_id=request.tool_call_id,
        note=request.note,
        error=request.error,
        created_at=request.created_at,
        decided_at=request.decided_at,
        applied_at=request.applied_at,
        resumed_at=request.resumed_at,
        needs_role=needs,
        can_decide=request.status == SchemaChangeStatus.pending and decides(role, needs),
        confirm_with=confirmation_for(request),
    )


def confirmation_for(request: SchemaChangeRequest) -> str | None:
    return confirmation(SchemaChange.model_validate({"ops": request.ops}))


def request_run(run: AgentRun) -> RequestRun:
    return RequestRun(agent_id=run.agent_id, run_id=run.id, title=run.title)


def request_runs(store: Session, requests: list[SchemaChangeRequest]) -> dict[int, RequestRun]:
    """The runs that made these requests (all the store's, `store`), by id: those that still
    exist."""
    ids = {r.requested_by_run for r in requests if r.requested_by_run is not None}
    if not ids:
        return {}
    runs = store.scalars(select(AgentRun).where(AgentRun.id.in_(ids))).all()
    return {run.id: request_run(run) for run in runs}


def decider_role(db: Session, user_id: int, store: DataStore) -> Role | None:
    """The user's role, if they may approve and reject the store's requests at all: they see
    them (`can_approve`), and their role may decide some (`aios.permissions`). Which they may
    decide is each request's `needs_role` (`decides`)."""
    if not can_approve(db, user_id, store):
        return None
    role = organisations.role(db, store.owner_organisation_id, user_id)
    return role if can(role, Action.decide) else None


def decides(role: Role | None, needs: Role) -> bool:
    """Whether a member with `role` (from `decider_role`) may decide a request needing `needs`."""
    return role is not None and approval_rules.at_least(role, needs)


def of_run(run_id: int) -> tuple[ColumnElement[bool], ...]:
    """Requests the run made (in its store)."""
    return (SchemaChangeRequest.requested_by_run == run_id,)


def run_requests(db: Session, store: Session, run: AgentRun, user_id: int) -> list[SchemaChangeOut]:
    """The requests the run (of the store `store` is a session on) made, oldest first (for its
    chat)."""
    requests = store.scalars(
        select(SchemaChangeRequest).where(*of_run(run.id)).order_by(SchemaChangeRequest.created_at)
    ).all()
    if not requests:
        return []
    store_id = store_id_of(store)
    # One run's requests are all on its store: whether the user may decide them is asked once.
    role = decider_role(db, user_id, db.get_one(DataStore, store_id))
    rules = approval_rules.current(store)
    return [request_out(store_id, request, request_run(run), role, rules) for request in requests]


def waiting_on(store: Session, run_id: int) -> bool:
    """Whether the run (of the store `store` is a session on) waits on a request: not yet
    resumed with its outcome."""
    return (
        store.scalar(
            select(func.count())
            .select_from(SchemaChangeRequest)
            .where(*of_run(run_id), *not_given())
        )
        > 0
    )


@router.get(
    "/schema-changes", response_model=list[SchemaChangeOut], dependencies=[allow(Action.read)]
)
def list_requests(user: CurrentUser, db: DB, store: CurrentStore):
    """The organisation's store's requests: pending first (oldest first), then the decided ones,
    most recently decided first."""
    store_id = store_id_of(store)
    data_store = db.get_one(DataStore, store_id)
    if not can_approve(db, user.id, data_store):
        return []
    pending = SchemaChangeRequest.status == SchemaChangeStatus.pending
    requests = store.scalars(
        select(SchemaChangeRequest).order_by(
            case((pending, 0), else_=1),
            case((pending, SchemaChangeRequest.created_at)),
            func.coalesce(SchemaChangeRequest.decided_at, SchemaChangeRequest.created_at).desc(),
        )
    ).all()
    role = decider_role(db, user.id, data_store)
    rules = approval_rules.current(store)
    runs = request_runs(store, list(requests))
    return [
        request_out(store_id, request, runs.get(request.requested_by_run), role, rules)
        for request in requests
    ]


def _get(
    db: Session, store: Session, user_id: int, request_id: uuid.UUID, *, lock: bool = False
) -> SchemaChangeRequest:
    """The request, if it's in the organisation's store (`store`) and the user may decide it: a
    404 otherwise, alike."""
    query = select(SchemaChangeRequest).where(SchemaChangeRequest.id == request_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    request = store.scalar(query)
    if request is None or not can_approve(db, user_id, db.get_one(DataStore, store_id_of(store))):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schema change request not found")
    return request


def _out(
    db: Session, store: Session, request: SchemaChangeRequest, user_id: int
) -> SchemaChangeOut:
    """A request `_get` found, for the user."""
    store_id = store_id_of(store)
    run = request_runs(store, [request]).get(request.requested_by_run)
    role = decider_role(db, user_id, db.get_one(DataStore, store_id))
    return request_out(store_id, request, run, role, approval_rules.current(store))


@router.get(
    "/schema-changes/{request_id}",
    response_model=SchemaChangeOut,
    dependencies=[allow(Action.read)],
)
def get_request(request_id: uuid.UUID, user: CurrentUser, db: DB, store: CurrentStore):
    return _out(db, store, _get(db, store, user.id, request_id), user.id)


def _undo(store: Session, request: SchemaChangeRequest) -> None:
    """Put a decision nothing else has seen through back to pending, auditing that it was;
    commits the store."""
    audit.record(
        store,
        audit.SYSTEM,
        "approval.undone",
        "schema_change_request",
        request.id,
        {"decision": request.status.value, "reason": "It couldn't be passed on"},
    )
    store.execute(
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
        .execution_options(synchronize_session=False)
    )
    store.commit()


def _decide(
    db: Session, store: Session, user_id: int, request_id: uuid.UUID, **values
) -> SchemaChangeRequest:
    # Locked, so two decisions at once can't both pass the pending check.
    request = _get(db, store, user_id, request_id, lock=True)
    if request.status != SchemaChangeStatus.pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"This request has already been {request.status.value}"
        )
    needs = _refuse_unless_decides(db, store, user_id, request)
    for key, value in values.items():
        setattr(request, key, value)
    request.decided_by_user_id = user_id
    request.decided_at = func.now()
    audit.record(
        store,
        audit.person(user_id),
        "approval.decided",
        "schema_change_request",
        request.id,
        {
            "decision": request.status.value,
            "summary": request.summary,
            **({"note": request.note} if request.note else {}),
            "needs_role": needs.value,
        },
    )
    # The store, the source of truth, first; then its notifications are cleared, as of the
    # decision (so undoing it can tell them apart from those a user read). If they can't be,
    # the decision is undone: they're cleared if and only if it's made.
    store.commit()
    store.refresh(request)
    try:
        clear_notifications(db, [request.id], at=request.decided_at)
        db.commit()
    except Exception:
        db.rollback()
        _undo(store, request)
        raise
    return request


def _refuse_unless_decides(
    db: Session, store: Session, user_id: int, request: SchemaChangeRequest
) -> Role:
    """The role the request needs, if the user's is at least that; a 403 naming it otherwise."""
    needs = approval_rules.needed_for(store, request.ops)
    role = decider_role(db, user_id, db.get_one(DataStore, store_id_of(store)))
    if not decides(role, needs):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This request needs {approval_rules.a(needs)} to approve or reject it"
            " (the organisation's approval rules). Ask one.",
        )
    return needs


def _queue(
    db: Session, store: Session, request: SchemaChangeRequest, queue: Callable[[], None]
) -> None:
    """Queue what follows a decision; if it can't be (the broker is down), undo the decision,
    which nothing else has seen through, so it's not left decided with nothing to act on it, and
    its notifications are unread again."""
    try:
        queue()
    except Exception as exc:
        log.exception("Couldn't queue what follows deciding schema change request %s", request.id)
        _undo(store, request)
        restore_notifications(db, request.id, request.decided_at)
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The decision couldn't be passed on just now, so it wasn't made. Try again.",
        ) from exc


@router.get(
    "/schema-changes/{request_id}/impact",
    response_model=ImpactOut,
    dependencies=[allow(Action.read)],
)
def get_impact(request_id: uuid.UUID, user: CurrentUser, db: DB, store: CurrentStore):
    """What the request's drops would remove from the store as it is now (row counts, the
    columns affected), for the member deciding it; nothing for one that drops nothing."""
    request = _get(db, store, user.id, request_id)
    try:
        removes = dropped.impact(store_id_of(store), request.ops)
    except (DataStoreBehind, DataStoreBusy, DataStoreNotFound, DataStoreNotReady) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The organisation's data store can't be read just now.",
        ) from exc
    return ImpactOut(removes=[RemovalOut(**vars(r)) for r in removes])


@router.post(
    "/schema-changes/{request_id}/approve",
    response_model=SchemaChangeOut,
    dependencies=[allow(Action.decide)],
)
def approve(
    request_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    store: CurrentStore,
    body: Approval | None = None,
):
    from aios.worker import apply_schema_change_task

    # Typed by the member deciding it, so a drop is never approved by a stray click (its ops
    # never change).
    found = _get(db, store, user.id, request_id)
    # Before asking for the confirmation: a member who can't decide it is told so first.
    _refuse_unless_decides(db, store, user.id, found)
    required = confirmation_for(found)
    typed = ((body.confirm if body else None) or "").strip()
    if required is not None and typed != required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"This request drops data: Type `{required}` to confirm.",
        )
    request = _decide(db, store, user.id, request_id, status=SchemaChangeStatus.approved)
    store_id = str(store_id_of(store))
    _queue(db, store, request, lambda: apply_schema_change_task.delay(store_id, str(request.id)))
    return _out(db, store, request, user.id)


@router.post(
    "/schema-changes/{request_id}/reject",
    response_model=SchemaChangeOut,
    dependencies=[allow(Action.decide)],
)
def reject(request_id: uuid.UUID, body: Rejection, user: CurrentUser, db: DB, store: CurrentStore):
    from aios.worker import enqueue_resume

    note = (body.note or "").strip() or None
    request = _decide(db, store, user.id, request_id, status=SchemaChangeStatus.rejected, note=note)
    store_id = store_id_of(store)
    _queue(db, store, request, lambda: enqueue_resume(store_id, request.id))
    return _out(db, store, request, user.id)
