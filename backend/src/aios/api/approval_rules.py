"""Approval rules (`aios.approval_rules`): read by every member, raised by admins (a rule can't
be lowered below its default: 422)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from aios import approval_rules
from aios.auth import CurrentStore, CurrentUser
from aios.datastores.models import ApprovalRule
from aios.models import Role
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")


class ApprovalRuleOut(BaseModel):
    kind: str
    # What it is, for the Organisation page.
    label: str
    # The least role that may decide it now, and by default (it can't go below that).
    role: Role
    default_role: Role
    updated_at: datetime | None


class ApprovalRuleIn(BaseModel):
    role: Role


def rules_out(store) -> list[ApprovalRuleOut]:
    rules = approval_rules.current(store)
    updated = dict(store.execute(select(ApprovalRule.kind, ApprovalRule.updated_at)).all())
    return [
        ApprovalRuleOut(
            kind=kind,
            label=approval_rules.LABELS[kind],
            role=rules[kind],
            default_role=default,
            updated_at=updated.get(kind),
        )
        for kind, default in approval_rules.DEFAULTS.items()
    ]


@router.get(
    "/approval-rules", response_model=list[ApprovalRuleOut], dependencies=[allow(Action.read)]
)
def list_rules(store: CurrentStore):
    return rules_out(store)


@router.put(
    "/approval-rules/{kind}",
    response_model=list[ApprovalRuleOut],
    dependencies=[allow(Action.run_organisation)],
)
def set_rule(kind: str, body: ApprovalRuleIn, user: CurrentUser, store: CurrentStore):
    """Set the least role that may decide `kind` (audited). 404 for a kind there isn't; 422
    below its default. Answers every rule."""
    if kind not in approval_rules.DEFAULTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There's no such kind of change")
    try:
        approval_rules.set_rule(store, kind, body.role, user.id)
    except approval_rules.Lowered as refused:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(refused)) from None
    store.commit()
    return rules_out(store)
