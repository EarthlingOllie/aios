"""Approvals (`aios.approvals`), for the people who decide them: the Data page's Approvals list,
the approval card in an agent's chat, and a workflow run's approval step. Both kinds, schema
changes and workflow approval steps, are listed, read and decided here; the old schema change
routes are gone.

Approvals are in the organisation's store. Only users who pass `can_approve` for an approval's
store (every member of its organisation) see it; to anyone else it doesn't exist (404). Deciding
one takes a role that may (editors and admins, `aios.permissions`), and at least the role it
needs: a schema change's operations' under the approval rules (`aios.approval_rules`), a
workflow step's node's. Each says which (`approver_role`), and whether the user may
(`can_decide`). Each decision is audited (`aios.audit`). Approving a schema change queues it to
be applied on the urgent worker, which then resumes the run that asked; any other decision
resumes the run waiting on it straight away.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, case, func, select, update
from sqlalchemy.orm import Session

from aios import approval_rules, approvals, audit, organisations
from aios.api.provenance import UserOut, people
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    can_approve,
    dropped,
    store_id_of,
)
from aios.datastores.models import (
    AgentRun,
    SchemaChangeRequest,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from aios.datastores.schema_changes import (
    SchemaChange,
    confirmation,
)
from aios.db import DB
from aios.models import (
    ApprovalKind,
    DataStore,
    Role,
    SchemaChangeStatus,
)
from aios.permissions import Action, allow, can

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

NOT_FOUND = "Approval not found"


class RequestRun(BaseModel):
    """The agent run that asked for a schema change, for a link to it."""

    agent_id: int
    run_id: int
    title: str | None


class StepOut(BaseModel):
    """The workflow approval step waiting on an approval: its workflow, run and node."""

    workflow_id: int
    workflow_name: str
    run_id: int
    step_id: int
    # Node keys from the run's workflow down to the approval node.
    path: list[str]


class ApprovalOut(BaseModel):
    id: uuid.UUID
    kind: ApprovalKind
    data_store_id: uuid.UUID
    status: SchemaChangeStatus
    # One line: a schema change's operations, or an approval step's prompt's first line.
    summary: str
    # Why it's asked, in full: the agent's explanation, or the approval step's rendered prompt.
    reason: str
    # The least role that may decide it: a workflow step's node's role, or the highest a schema
    # change's operations need (`aios.approval_rules`). `needs_role` is the same.
    approver_role: Role
    needs_role: Role
    # Whether the signed-in user may approve or reject it (and it's still pending).
    can_decide: bool
    note: str | None
    created_at: datetime
    decided_at: datetime | None
    # The member who decided it; None while it's pending, and for one that expired or was
    # cancelled.
    decided_by: UserOut | None = None
    # When it expires undecided, counting as not approved.
    expires_at: datetime | None = None
    # When the run waiting on it was given the outcome.
    resumed_at: datetime | None
    # A schema change's: exactly what runs on approval, the run that asked, and the tool call
    # it asked in (so a chat shows the request at that call); why applying failed; and what
    # approving it takes typed if it drops anything (the tables' names).
    sql: str | None = None
    ops: list[dict[str, Any]] | None = None
    run: RequestRun | None = None
    tool_call_id: str | None = None
    error: str | None = None
    applied_at: datetime | None = None
    confirm_with: str | None = None
    # A workflow step's: what its node was given, and the step (its workflow, run and node).
    input: Any = None
    step: StepOut | None = None


# The old name, for the agents' run pages.
SchemaChangeOut = ApprovalOut


class Approval(BaseModel):
    # For a schema change that drops anything, its `confirm_with`, typed.
    confirm: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=2000)


class RemovalOut(BaseModel):
    """What one of a schema change's drops would remove, as the store is now."""

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


def _is_schema_change(approval: SchemaChangeRequest) -> bool:
    return approval.kind == ApprovalKind.schema_change


def approval_out(
    data_store_id: uuid.UUID,
    approval: SchemaChangeRequest,
    role: Role | None,
    rules: dict[str, Role],
    *,
    run: RequestRun | None = None,
    step: StepOut | None = None,
    deciders: dict[int, UserOut] | None = None,
) -> ApprovalOut:
    """The approval, for a member with `role` (None for one who may decide none of its store's
    approvals), under the store's approval `rules`: they may decide it only while it's pending,
    and if their role is at least what it needs."""
    needs = approvals.needs_role(rules, approval)
    schema_change = _is_schema_change(approval)
    decider = approval.decided_by_user_id
    return ApprovalOut(
        id=approval.id,
        kind=ApprovalKind(approval.kind),
        data_store_id=data_store_id,
        status=approval.status,
        summary=approval.summary or "",
        reason=approval.reason,
        approver_role=needs,
        needs_role=needs,
        can_decide=approval.status == SchemaChangeStatus.pending and decides(role, needs),
        note=approval.note,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
        decided_by=(deciders or {}).get(decider) if decider is not None else None,
        expires_at=approval.expires_at,
        resumed_at=approval.resumed_at,
        sql=approval.sql,
        ops=approval.ops,
        run=run,
        tool_call_id=approval.tool_call_id,
        error=approval.error,
        applied_at=approval.applied_at,
        confirm_with=confirmation_for(approval) if schema_change else None,
        input=approval.input,
        step=step,
    )


def confirmation_for(request: SchemaChangeRequest) -> str | None:
    if not _is_schema_change(request):
        return None
    return confirmation(SchemaChange.model_validate({"ops": request.ops}))


def request_run(run: AgentRun) -> RequestRun:
    return RequestRun(agent_id=run.agent_id, run_id=run.id, title=run.title)


def request_runs(store: Session, requests: list[SchemaChangeRequest]) -> dict[int, RequestRun]:
    """The runs that asked for these schema changes (all the store's, `store`), by id: those
    that still exist."""
    ids = {r.requested_by_run for r in requests if r.requested_by_run is not None}
    if not ids:
        return {}
    runs = store.scalars(select(AgentRun).where(AgentRun.id.in_(ids))).all()
    return {run.id: request_run(run) for run in runs}


def approval_steps(store: Session, found: list[SchemaChangeRequest]) -> dict[uuid.UUID, StepOut]:
    """The workflow steps waiting on these approvals (all the store's), by approval id: those
    that still exist."""
    step_ids = {a.workflow_step_id for a in found if a.workflow_step_id is not None}
    if not step_ids:
        return {}
    rows = store.execute(
        select(SchemaChangeRequest.id, WorkflowRunStep, WorkflowRun.workflow_id, Workflow.name)
        .join(WorkflowRunStep, WorkflowRunStep.id == SchemaChangeRequest.workflow_step_id)
        .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(WorkflowRunStep.id.in_(step_ids))
    ).all()
    return {
        approval_id: StepOut(
            workflow_id=workflow_id,
            workflow_name=name,
            run_id=step.run_id,
            step_id=step.id,
            path=step.path,
        )
        for approval_id, step, workflow_id, name in rows
    }


def approvals_out(
    db: Session,
    organisation_id: int,
    store: Session,
    found: list[SchemaChangeRequest],
    role: Role | None,
) -> list[ApprovalOut]:
    """`approval_out` for each of the store's (`store`'s) approvals, reading what they refer
    to all at once."""
    store_id = store_id_of(store)
    rules = approval_rules.current(store)
    runs = request_runs(store, found)
    steps = approval_steps(store, found)
    deciders = people(db, organisation_id, {a.decided_by_user_id for a in found})
    return [
        approval_out(
            store_id,
            a,
            role,
            rules,
            run=runs.get(a.requested_by_run),
            step=steps.get(a.id),
            deciders=deciders,
        )
        for a in found
    ]


def decider_role(db: Session, user_id: int, store: DataStore) -> Role | None:
    """The user's role, if they may approve and reject the store's approvals at all: they see
    them (`can_approve`), and their role may decide some (`aios.permissions`). Which they may
    decide is each one's `approver_role` (`decides`)."""
    if not can_approve(db, user_id, store):
        return None
    role = organisations.role(db, store.owner_organisation_id, user_id)
    return role if can(role, Action.decide) else None


def decides(role: Role | None, needs: Role) -> bool:
    """Whether a member with `role` (from `decider_role`) may decide an approval needing
    `needs`."""
    return role is not None and approval_rules.at_least(role, needs)


def of_run(run_id: int) -> tuple[ColumnElement[bool], ...]:
    """Schema changes the run asked for (in its store)."""
    return (SchemaChangeRequest.requested_by_run == run_id,)


def run_requests(db: Session, store: Session, run: AgentRun, user_id: int) -> list[ApprovalOut]:
    """The schema changes the run (of the store `store` is a session on) asked for, oldest first
    (for its chat)."""
    requests = list(
        store.scalars(
            select(SchemaChangeRequest)
            .where(*of_run(run.id))
            .order_by(SchemaChangeRequest.created_at)
        ).all()
    )
    if not requests:
        return []
    store_id = store_id_of(store)
    data_store = db.get_one(DataStore, store_id)
    # One run's requests are all on its store: whether the user may decide them is asked once.
    role = decider_role(db, user_id, data_store)
    return approvals_out(db, data_store.owner_organisation_id, store, requests, role)


def waiting_on(store: Session, run_id: int) -> bool:
    """Whether the agent run (of the store `store` is a session on) waits on a schema change:
    not yet resumed with its outcome."""
    return (
        store.scalar(
            select(func.count())
            .select_from(SchemaChangeRequest)
            .where(*of_run(run_id), *approvals.not_given())
        )
        > 0
    )


@router.get("/approvals", response_model=list[ApprovalOut], dependencies=[allow(Action.read)])
def list_approvals(
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    kind: ApprovalKind | None = None,
    status_: Annotated[SchemaChangeStatus | None, Query(alias="status")] = None,
):
    """The organisation's store's approvals, of a `kind` and in a `status` if given: pending
    first (oldest first), then the decided ones, most recently decided first."""
    store_id = store_id_of(store)
    data_store = db.get_one(DataStore, store_id)
    if not can_approve(db, user.id, data_store):
        return []
    pending = SchemaChangeRequest.status == SchemaChangeStatus.pending
    query = select(SchemaChangeRequest).order_by(
        case((pending, 0), else_=1),
        case((pending, SchemaChangeRequest.created_at)),
        func.coalesce(SchemaChangeRequest.decided_at, SchemaChangeRequest.created_at).desc(),
    )
    if kind is not None:
        query = query.where(SchemaChangeRequest.kind == kind.value)
    if status_ is not None:
        query = query.where(SchemaChangeRequest.status == status_)
    found = list(store.scalars(query).all())
    return approvals_out(db, org.id, store, found, decider_role(db, user.id, data_store))


def _get(
    db: Session, store: Session, user_id: int, approval_id: uuid.UUID, *, lock: bool = False
) -> SchemaChangeRequest:
    """The approval, if it's in the organisation's store (`store`) and the user may see it: a
    404 otherwise, alike."""
    query = select(SchemaChangeRequest).where(SchemaChangeRequest.id == approval_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    found = store.scalar(query)
    if found is None or not can_approve(db, user_id, db.get_one(DataStore, store_id_of(store))):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return found


def _out(
    db: Session, organisation_id: int, store: Session, found: SchemaChangeRequest, user_id: int
) -> ApprovalOut:
    """An approval `_get` found, for the user."""
    role = decider_role(db, user_id, db.get_one(DataStore, store_id_of(store)))
    return approvals_out(db, organisation_id, store, [found], role)[0]


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalOut,
    dependencies=[allow(Action.read)],
)
def get_approval(
    approval_id: uuid.UUID, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    return _out(db, org.id, store, _get(db, store, user.id, approval_id), user.id)


def _target(found: SchemaChangeRequest) -> str:
    """What an approval's audit entries are about."""
    return "schema_change_request" if _is_schema_change(found) else "approval"


def _undo(store: Session, found: SchemaChangeRequest) -> None:
    """Put a decision nothing else has seen through back to pending, auditing that it was;
    commits the store."""
    audit.record(
        store,
        audit.SYSTEM,
        "approval.undone",
        _target(found),
        found.id,
        {"decision": found.status.value, "reason": "It couldn't be passed on"},
    )
    store.execute(
        update(SchemaChangeRequest)
        .where(
            SchemaChangeRequest.id == found.id,
            SchemaChangeRequest.status == found.status,
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
    db: Session, store: Session, user_id: int, approval_id: uuid.UUID, **values
) -> SchemaChangeRequest:
    # Locked, so two decisions at once can't both pass the pending check.
    found = _get(db, store, user_id, approval_id, lock=True)
    if found.status != SchemaChangeStatus.pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"This approval has already been {found.status.value}"
        )
    needs = _refuse_unless_decides(db, store, user_id, found)
    for key, value in values.items():
        setattr(found, key, value)
    found.decided_by_user_id = user_id
    found.decided_at = func.now()
    audit.record(
        store,
        audit.person(user_id),
        "approval.decided",
        _target(found),
        found.id,
        {
            "kind": found.kind,
            "decision": found.status.value,
            "summary": found.summary,
            **({"note": found.note} if found.note else {}),
            "needs_role": needs.value,
            **(
                {"workflow_step_id": found.workflow_step_id}
                if found.workflow_step_id is not None
                else {}
            ),
        },
    )
    # The store, the source of truth, first; then its notifications are cleared, as of the
    # decision (so undoing it can tell them apart from those a user read). If they can't be,
    # the decision is undone: they're cleared if and only if it's made.
    store.commit()
    store.refresh(found)
    try:
        approvals.clear_notifications(db, [found.id], at=found.decided_at)
        db.commit()
    except Exception:
        db.rollback()
        _undo(store, found)
        raise
    return found


def _refuse_unless_decides(
    db: Session, store: Session, user_id: int, found: SchemaChangeRequest
) -> Role:
    """The role the approval needs, if the user's is at least that; a 403 naming it
    otherwise."""
    needs = approvals.needs_role_in(store, found)
    role = decider_role(db, user_id, db.get_one(DataStore, store_id_of(store)))
    if not decides(role, needs):
        why = (
            "the organisation's approval rules"
            if _is_schema_change(found)
            else "its workflow's approval step"
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This approval needs {approval_rules.a(needs)} to approve or reject it"
            f" ({why}). Ask one.",
        )
    return needs


def _queue(
    db: Session, store: Session, found: SchemaChangeRequest, queue: Callable[[], None]
) -> None:
    """Queue what follows a decision; if it can't be (the broker is down), undo the decision,
    which nothing else has seen through, so it's not left decided with nothing to act on it, and
    its notifications are unread again."""
    try:
        queue()
    except Exception as exc:
        log.exception("Couldn't queue what follows deciding approval %s", found.id)
        _undo(store, found)
        approvals.restore_notifications(db, found.id, found.decided_at)
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The decision couldn't be passed on just now, so it wasn't made. Try again.",
        ) from exc


@router.get(
    "/approvals/{approval_id}/impact",
    response_model=ImpactOut,
    dependencies=[allow(Action.read)],
)
def get_impact(approval_id: uuid.UUID, user: CurrentUser, db: DB, store: CurrentStore):
    """What a schema change's drops would remove from the store as it is now (row counts, the
    columns affected), for the member deciding it; nothing for one that drops nothing. A 422 for
    another kind of approval."""
    found = _get(db, store, user.id, approval_id)
    if not _is_schema_change(found):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Only a schema change has an impact."
        )
    try:
        removes = dropped.impact(store_id_of(store), found.ops or [])
    except (DataStoreBehind, DataStoreBusy, DataStoreNotFound, DataStoreNotReady) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The organisation's data store can't be read just now.",
        ) from exc
    return ImpactOut(removes=[RemovalOut(**vars(r)) for r in removes])


def _note(text: str | None) -> str | None:
    return (text or "").strip() or None


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalOut,
    dependencies=[allow(Action.decide)],
)
def approve(
    approval_id: uuid.UUID,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    body: Approval | None = None,
):
    from aios.worker import apply_schema_change_task, enqueue_resume

    # Typed by the member deciding it, so a drop is never approved by a stray click (its ops
    # never change).
    found = _get(db, store, user.id, approval_id)
    # Before asking for the confirmation: a member who can't decide it is told so first.
    _refuse_unless_decides(db, store, user.id, found)
    required = confirmation_for(found)
    typed = ((body.confirm if body else None) or "").strip()
    if required is not None and typed != required:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"This request drops data: Type `{required}` to confirm.",
        )
    decided = _decide(
        db,
        store,
        user.id,
        approval_id,
        status=SchemaChangeStatus.approved,
        note=_note(body.note if body else None),
    )
    store_id = store_id_of(store)
    if _is_schema_change(decided):
        _queue(
            db,
            store,
            decided,
            lambda: apply_schema_change_task.delay(str(store_id), str(decided.id)),
        )
    else:
        _queue(db, store, decided, lambda: enqueue_resume(store_id, decided.id))
    return _out(db, org.id, store, decided, user.id)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalOut,
    dependencies=[allow(Action.decide)],
)
def reject(
    approval_id: uuid.UUID,
    body: Rejection,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    from aios.worker import enqueue_resume

    decided = _decide(
        db,
        store,
        user.id,
        approval_id,
        status=SchemaChangeStatus.rejected,
        note=_note(body.note),
    )
    store_id = store_id_of(store)
    _queue(db, store, decided, lambda: enqueue_resume(store_id, decided.id))
    return _out(db, org.id, store, decided, user.id)
