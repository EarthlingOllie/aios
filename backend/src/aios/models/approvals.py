"""What approvals can be (`aios.approvals`): schema changes an agent asked for, and workflow
approval steps.

Approvals are in each organisation's store (`aios.datastores.models.Approval`); the platform's
`approval_needed` notifications name them by id, from the store's organisation.
"""

import enum


class ApprovalKind(enum.StrEnum):
    # An agent's request to change the data schema (`aios.datastores.schema_changes`).
    schema_change = "schema_change"
    # A workflow's approval node waiting for a person (`aios.workflows`).
    workflow_step = "workflow_step"


class ApprovalStatus(enum.StrEnum):
    """An approval's status. Shared: pending, approved, rejected, expired, cancelled. A schema
    change's only: applied, failed, stale."""

    pending = "pending"  # waiting for a member to decide it
    approved = "approved"  # a schema change: being applied
    rejected = "rejected"
    expired = "expired"  # undecided for too long; counts as not approved
    applied = "applied"
    failed = "failed"  # applying it failed; the schema is unchanged
    stale = "stale"  # the schema changed since, and it no longer applies
    cancelled = "cancelled"  # the run that asked was deleted or cancelled, or can't resume

    @property
    def decided(self) -> bool:
        """Whether this is an outcome the run that asked for a schema change can be resumed
        with (an approved one is still being applied). See `decided` for any kind."""
        return self not in (ApprovalStatus.pending, ApprovalStatus.approved)


def decided(kind: str, status: ApprovalStatus) -> bool:
    """Whether an approval of `kind` has an outcome its run can be given: anything but pending
    for a workflow step; anything but pending or approved (being applied) for a schema
    change."""
    if kind == ApprovalKind.workflow_step:
        return status != ApprovalStatus.pending
    return ApprovalStatus(status).decided
