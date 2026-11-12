"""Approvals: something a member with the right role must decide, approving or rejecting it,
before a run can carry on. One mechanism for both kinds (`aios.models.ApprovalKind`):

- **Schema changes**, which an agent asks for (`aios.datastores.schema_changes` keeps checking,
  compiling and applying them, and calls in here). Who may decide one is the organisation's
  approval rules' (`aios.approval_rules`): the highest role its operations need.
- **Workflow approval steps**, which a workflow's approval node makes when it's reached
  (`aios.workflows`): its rendered prompt and its input, decided by its node's role (editor or
  admin) or above. On decision, the node outputs `outcome`, which edges can branch on.

This module owns what they share: creating one with its expiry time (`expires_at`, from the
organisation's setting or the node's own), notifying those who may decide it, and only them
(`notify`; phase 2's `approval_needed`), whether an outcome is ready for the run (`decided`),
expiring those left undecided (`expire`; counting as not approved), cancelling those nothing
waits on any more (`cancel`), clearing their notifications once they no longer wait
(`clear_notifications`), and resuming the run waiting on one (`resume`).

Approvals are in their store, the source of truth, and notifications on the platform, which name
each by its id, from its store's organisation: an approval is closed in its store first, and its
notifications are cleared once that has committed (`clear_notifications_of`). Still kept in
`schema_change_requests` (`SchemaChangeRequest`) until the rename to approvals.
"""

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.orm import Session

from aios import approval_rules, notifications
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
from aios.models import (
    ApprovalKind,
    DataStore,
    Organisation,
    Role,
    SchemaChangeStatus,
)
from aios.models.approvals import decided as status_decided

log = logging.getLogger(__name__)

# An approval, named outside its store: the store, and the approval's id.
StoredRequest = tuple[uuid.UUID, uuid.UUID]

Approval = SchemaChangeRequest

# The longest an approval node may set its own expiry to, in days.
MAX_EXPIRY_DAYS = 365

# What an approval node outputs once it's decided (`outcome`): the schema its outgoing edges'
# conditions read.
OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "note": {"type": ["string", "null"]},
        "decided_by": {
            "type": ["object", "null"],
            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        },
        "status": {"enum": ["approved", "rejected", "expired", "cancelled"]},
    },
    "required": ["approved", "note", "decided_by", "status"],
}

# How long a summary (an approval step's prompt's first line) may be.
SUMMARY_LENGTH = 200


# --- Creating -----------------------------------------------------------------------------------


def expiry_days(db: Session, organisation_id: int | None) -> int:
    """The organisation's approval expiry setting, in days (7 if it has none)."""
    days = (
        db.scalar(
            select(Organisation.approval_expiry_days).where(Organisation.id == organisation_id)
        )
        if organisation_id is not None
        else None
    )
    return days or 7


def store_expiry_days(data_store_id: uuid.UUID) -> int:
    """`expiry_days` for the organisation that owns the store."""
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        return expiry_days(db, organisation_id)


def expires_in(days: float) -> Any:
    """An expiry time `days` from now, as the database's clock has it."""
    return func.now() + timedelta(days=days)


def summary_of(prompt: str) -> str:
    """An approval step's one-line summary: its prompt's first non-blank line, cut short."""
    line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "") or "Approval"
    return line if len(line) <= SUMMARY_LENGTH else line[: SUMMARY_LENGTH - 1] + "…"


def create_step_approval(
    store: Session,
    step: WorkflowRunStep,
    *,
    prompt: str,
    input: Any,
    role: Role,
    days: float,
) -> Approval:
    """Record a pending approval for a workflow's approval node, at its step, with the prompt it
    rendered and its input, decided by `role` or above, expiring `days` from now; and make the
    step wait on it. In the caller's transaction (an advance's): notify those who may decide it
    (`notify`) once that commits."""
    approval = Approval(
        kind=ApprovalKind.workflow_step.value,
        summary=summary_of(prompt),
        reason=prompt,
        input=input,
        approver_role=role.value,
        expires_at=expires_in(days),
        workflow_step_id=step.id,
    )
    store.add(approval)
    store.flush()
    step.approval_id = approval.id
    return approval


# --- Who may decide ------------------------------------------------------------------------------


def needs_role(rules: dict[str, Role], approval: Approval) -> Role:
    """The least role that may decide the approval: a workflow step's node's role; the highest
    a schema change's operations need under the store's approval `rules`."""
    if approval.kind == ApprovalKind.workflow_step:
        return Role(approval.approver_role or Role.editor)
    return approval_rules.needed(rules, approval.ops or [])


def needs_role_in(store: Session, approval: Approval) -> Role:
    """`needs_role`, under the store's (`store`'s) current approval rules."""
    if approval.kind == ApprovalKind.workflow_step:
        return needs_role({}, approval)
    return approval_rules.needed_for(store, approval.ops or [])


def notify(data_store_id: uuid.UUID, approvals: Iterable[tuple[uuid.UUID, Role]]) -> None:
    """Tell those who may decide each approval of the store (`(id, least role)`) that it's
    waiting for them, and only them: the members whose role is at least its role. On the
    platform, once the store has committed it; committed. One that fails is logged: the
    approval waits either way, and is in the Approvals list."""
    from aios.datastores.resolve import approvers

    approvals = list(approvals)
    if not approvals:
        return
    try:
        with SessionLocal() as db:
            data_store = db.get(DataStore, data_store_id)
            if data_store is None or data_store.owner_organisation_id is None:
                return
            for approval_id, least in approvals:
                notifications.notify(
                    db,
                    approvers(db, data_store, least),
                    data_store.owner_organisation_id,
                    notifications.APPROVAL_NEEDED,
                    notifications.approval_ref(approval_id),
                )
            db.commit()
    except Exception:
        log.exception("couldn't notify the approvers of %s", [a for a, _ in approvals])


# --- Outcomes -----------------------------------------------------------------------------------


def decided(approval: Approval) -> bool:
    """Whether the approval has an outcome its run can be given (`aios.models.approvals`)."""
    return status_decided(approval.kind, approval.status)


def decided_clause() -> ColumnElement[bool]:
    """`decided`, in SQL."""
    return or_(
        and_(
            SchemaChangeRequest.kind == ApprovalKind.workflow_step.value,
            SchemaChangeRequest.status != SchemaChangeStatus.pending,
        ),
        SchemaChangeRequest.status.not_in(
            [SchemaChangeStatus.pending, SchemaChangeStatus.approved]
        ),
    )


def person_name(organisation_id: int, user_id: int | None) -> str | None:
    """A member's name (or a former member's), from the organisation's people mirror, or the
    platform for one it hasn't caught up with (`aios.datastores.people.resolve`)."""
    from aios.datastores import people

    if user_id is None:
        return None
    with SessionLocal() as db:
        found = people.resolve(db, organisation_id, [user_id]).get(user_id)
    return found.name if found is not None else None


def outcome(approval: Approval, organisation_id: int) -> dict[str, Any]:
    """What an approval node outputs once its approval is decided (`OUTCOME_SCHEMA`): whether
    it was approved (an expired or cancelled one wasn't), the decider's note, who decided it
    (none for an expired or cancelled one), and how it ended."""
    status = SchemaChangeStatus(approval.status)
    decider = approval.decided_by_user_id
    return {
        "approved": status == SchemaChangeStatus.approved,
        "note": approval.note,
        "decided_by": None
        if decider is None
        else {"id": decider, "name": person_name(organisation_id, decider) or "A former member"},
        "status": status.value,
    }


# --- Closing ------------------------------------------------------------------------------------


def not_given() -> list[ColumnElement[bool]]:
    """An approval the run waiting on it still waits on: not yet given its outcome, nor
    cancelled (whose run has been told, or is gone)."""
    return [
        SchemaChangeRequest.resumed_at.is_(None),
        SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
    ]


def clear_notifications(
    db: Session, request_ids: Iterable[uuid.UUID], at: datetime | None = None
) -> None:
    """Mark the approvals' notifications read (as of `at`, else now), for everyone they
    notified: the approvals no longer wait for a decision (they're decided, cancelled or
    expired). In the caller's transaction."""
    # One by one, each by the jsonb index; there are seldom more than a few at once.
    for request_id in request_ids:
        notifications.mark_read_about(
            db, notifications.APPROVAL_NEEDED, notifications.approval_ref(request_id), at
        )


def clear_notifications_of(request_ids: Iterable[uuid.UUID]) -> None:
    """`clear_notifications` for approvals their store has just closed, committed. One that
    fails is logged: the approvals are closed either way, and their notifications only say
    less than they might."""
    request_ids = list(request_ids)
    if not request_ids:
        return
    try:
        with SessionLocal() as db:
            clear_notifications(db, request_ids)
            db.commit()
    except Exception:
        log.exception("couldn't clear the notifications of approvals %s", request_ids)


def restore_notifications(db: Session, request_id: uuid.UUID, cleared_at: datetime) -> None:
    """Undo `clear_notifications` for a decision that was undone (the approval waits again):
    those it marked read as of `cleared_at` are unread again; any a user marked read themselves
    stay read. In the caller's transaction."""
    notifications.mark_unread_about(
        db, notifications.APPROVAL_NEEDED, notifications.approval_ref(request_id), cleared_at
    )


def _close(
    store: Session, status: SchemaChangeStatus, *where: ColumnElement[bool], **values: Any
) -> list[uuid.UUID]:
    """Take the store's pending approvals matching `where` out of `pending` with `status` (and
    `values`); returns their ids, whose notifications to clear once this commits
    (`clear_notifications_of`). Only one still pending is closed, in one statement: one being
    decided meanwhile holds its row (`aios.api.approvals`), and this waits for it, then leaves
    it. In the caller's transaction."""
    return list(
        store.scalars(
            update(SchemaChangeRequest)
            .where(*where, SchemaChangeRequest.status == SchemaChangeStatus.pending)
            .values(status=status, decided_at=func.now(), **values)
            .returning(SchemaChangeRequest.id)
        ).all()
    )


def cancel(store: Session, *where: ColumnElement[bool]) -> list[uuid.UUID]:
    """Cancel the store's pending approvals matching `where`; returns their ids, whose
    notifications to clear once this commits (`clear_notifications_of`). Nothing will resume the
    runs waiting on them (they're gone, cancelled, or have been told the call didn't finish), so
    they count as given. In the caller's transaction."""
    return _close(store, SchemaChangeStatus.cancelled, *where, resumed_at=func.now())


def overdue() -> ColumnElement[bool]:
    """A pending approval past its expiry time; one made before expiry times were recorded,
    past the deployment's schema change expiry."""
    age = timedelta(days=get_settings().schema_change_request_expiry_days)
    return or_(
        SchemaChangeRequest.expires_at < func.now(),
        and_(
            SchemaChangeRequest.expires_at.is_(None),
            SchemaChangeRequest.created_at < func.now() - age,
        ),
    )


def expire() -> list[StoredRequest]:
    """Expire the approvals, in every ready store, still pending past their expiry time,
    clearing their notifications; returns them. Expiring counts as not approved: the runs
    waiting on them are then resumed with it (`aios.worker.expire_schema_change_requests_task`).
    A store that can't be reached is left for the next run."""
    expired: list[StoredRequest] = []
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                ids = _close(store, SchemaChangeStatus.expired, overdue())
                store.commit()
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy) as exc:
            log.warning("couldn't expire the approvals of data store %s: %s", store_id, exc)
            continue
        clear_notifications_of(ids)
        expired.extend((store_id, request_id) for request_id in ids)
    return expired


def expired_not_resumed() -> list[StoredRequest]:
    """Expired approvals, in every ready store, whose runs haven't been given the outcome yet:
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
                    WorkflowRunStep.approval_id == SchemaChangeRequest.id
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


# --- Resuming -----------------------------------------------------------------------------------


def resume(data_store_id: uuid.UUID, approval_id: uuid.UUID) -> None:
    """Carry on the run waiting on a decided approval of the store, if it hasn't been given the
    outcome yet: a workflow run's advance, or an agent run's resume turn
    (`aios.run_service.work.enqueue_resume`)."""
    from aios.run_service import work

    work.enqueue_resume(data_store_id, approval_id)
