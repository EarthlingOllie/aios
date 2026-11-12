"""The organisation's budget and allowances (`aios.budgets`), in estimated pounds sterling per
calendar month in UTC.

`GET .../organisation/budget`: the budget (null for none), this month's spend against it, the
warning threshold, each role's default allowance, and each member's allowance (their own, and
the one in force) and spend this month. Every member reads the budget; each member's spend is
admins' only, unless an admin shows usage by person to everyone (as on Insights,
`PUT .../organisation/insights-people`): anyone else gets only their own.

Admins only (`aios.permissions`), each taking effect at once and audited when it changes:
`PUT .../organisation/budget` sets the budget and the role allowances (null for none);
`PUT .../organisation/members/{user_id}/allowance` sets a member's own allowance in place of
their role's (null to follow the role's again; a 404 for someone who isn't a member).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from aios import audit, budgets
from aios.api.provenance import UserOut, people
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import store_id_of
from aios.db import DB
from aios.models import Organisation, OrganisationMember, Role
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api")

NOT_A_MEMBER = "They aren't a member of this organisation."

# Pounds: none below zero, and at most a billion.
Amount = Annotated[float, Field(ge=0, le=1_000_000_000, allow_inf_nan=False)]


class MemberSpendOut(BaseModel):
    user: UserOut
    role: Role
    # Their own allowance, an admin's override; null follows their role's.
    allowance: float | None
    # What counts: their own, else their role's, else none (null).
    allowance_in_force: float | None
    spent: float


class BudgetOut(BaseModel):
    # This calendar month (UTC), as YYYY-MM.
    month: str
    budget: float | None
    spent: float
    # Admins are warned once a month when spending passes this share of the budget.
    warning_threshold: float
    role_allowances: dict[Role, float | None]
    # Every member's, or, for a member who may not see everyone's, only their own.
    members: list[MemberSpendOut]
    everyone: bool


class BudgetIn(BaseModel):
    budget: Amount | None
    # A role left out has no allowance.
    role_allowances: dict[Role, Amount | None] = {}


class AllowanceIn(BaseModel):
    allowance: Amount | None


def budget_out(
    db: DB, store: CurrentStore, org: Organisation, viewer: int, role: Role
) -> BudgetOut:
    everyone = role == Role.admin or org.insights_people_visible
    found = budgets.overview(db, store, org, viewer, everyone)
    names = people(db, org.id, [m["user_id"] for m in found["members"]])
    members = [
        MemberSpendOut(user=names[m.pop("user_id")], **m)
        for m in found["members"]
        if m["user_id"] in names
    ]
    return BudgetOut(**(found | {"members": members, "everyone": everyone}))


def _thresholds(data_store_id: uuid.UUID) -> None:
    """A budget lowered below this month's spend warns, or tells, the admins at once."""
    try:
        budgets.check_thresholds(data_store_id)
    except Exception:
        # The next spending checks again.
        budgets.log.exception("couldn't check the budget's thresholds")


@router.get("/organisation/budget", response_model=BudgetOut, dependencies=[allow(Action.read)])
def get_budget(
    user: CurrentUser, org: CurrentOrganisation, role: CurrentRole, db: DB, store: CurrentStore
):
    return budget_out(db, store, org, user.id, role)


@router.put(
    "/organisation/budget",
    response_model=BudgetOut,
    dependencies=[allow(Action.run_organisation)],
)
def set_budget(
    body: BudgetIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Set the monthly budget (null: none) and each role's default allowance, audited as each
    changes. They take effect at once: work is refused, or allowed again, from the next check."""
    old_budget, old_allowances = org.budget, dict(org.role_allowances or {})
    new_allowances = {
        r.value: amount for r, amount in body.role_allowances.items() if amount is not None
    }
    org.budget = body.budget
    org.role_allowances = new_allowances
    actor = audit.person(user.id)
    if body.budget != old_budget:
        audit.enqueue(
            db,
            org.id,
            actor,
            "settings.changed",
            "organisation",
            org.id,
            {"setting": "budget", "from": old_budget, "to": body.budget},
        )
    if new_allowances != old_allowances:
        audit.enqueue(
            db,
            org.id,
            actor,
            "settings.changed",
            "organisation",
            org.id,
            {"setting": "role_allowances", "from": old_allowances, "to": new_allowances},
        )
    db.commit()
    _thresholds(store_id_of(store))
    return budget_out(db, store, org, user.id, role)


@router.put(
    "/organisation/members/{user_id}/allowance",
    response_model=BudgetOut,
    dependencies=[allow(Action.run_organisation)],
)
def set_allowance(
    user_id: int,
    body: AllowanceIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Set a member's own monthly allowance in place of their role's (null: their role's
    again), audited when it changes; a 404 for someone who isn't a member."""
    member = db.get(OrganisationMember, (org.id, user_id), with_for_update=True)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_A_MEMBER)
    old = member.allowance
    member.allowance = body.allowance
    if body.allowance != old:
        audit.enqueue(
            db,
            org.id,
            audit.person(user.id),
            "member.allowance_changed",
            "member",
            user_id,
            {"from": old, "to": body.allowance},
        )
    db.commit()
    return budget_out(db, store, org, user.id, role)
