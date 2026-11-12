"""Budgets: a ceiling on what an organisation spends, in the usage ledger's estimated pounds
sterling (`aios.usage`), per calendar month in UTC.

- **Budget and allowances.** An admin sets the organisation's monthly budget (none by default:
  nothing is refused), default monthly allowances per role, and a member's own allowance in
  place of their role's (`aios.api.budget`). Changes take effect at once and are audited.
- **Totals.** `aios.spend_totals` keeps a running total per month for the organisation (person
  `ORGANISATION`) and per person, added to in the same transaction as each priced ledger entry
  (`add`, from `aios.usage.record`), so a check reads two rows rather than summing the ledger.
  A nightly job puts them right against the ledger, logging any drift (`reconcile_all`).
- **Attribution.** The ledger charges each entry to the person at the root of its chain of
  causes (`Invocation.charged_to`, `root_person`): whoever mentioned an agent, started a run by
  hand, or posted the message that fired a chat-message trigger, through any `start_workflow`
  calls and triggers that followed. A chain a schedule, or an event with no person behind it,
  began counts only against the organisation's budget.
- **Checking** (`refusal`) slots into the order of checks before new work (permission, rate,
  budget, then the handoff, `aios.guards`): at 100% of the budget, or of the root person's
  allowance, a mention is refused on its card, a trigger's firing on the trigger, and an agent's
  `start_workflow` call as the tool's result, all before any model is called. Runs already
  going, and resumes of waiting runs, aren't new work and finish.
- **Notifications.** Once a month each, every admin is warned when the organisation's spending
  passes `budget_warning_threshold` (80%) of the budget (`budget_warning`), and told when it
  reaches it (`budget_reached`), as the store commits the spending that did it
  (`check_thresholds`).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import notifications
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.connections import ready_stores, store_id_of
from aios.datastores.models import Event, Invocation, SpendTotal, UsageEntry
from aios.db import SessionLocal
from aios.models import DataStore, EventType, Organisation, OrganisationMember, Role, User

log = logging.getLogger(__name__)

# The person id of the organisation's own total.
ORGANISATION = 0

BUDGET_REACHED = (
    "The organisation has reached its monthly budget of £{amount}, so no new work starts until"
    " next month, or until an admin raises it."
)
ALLOWANCE_REACHED = (
    "{name} has reached their monthly allowance of £{amount}, so no new work they start runs"
    " until next month, or until an admin raises it."
)
SOMEONE = "The person who started this"


def pounds(amount: float) -> str:
    return f"{amount:,.2f}"


def this_month(now: datetime | None = None) -> date:
    """The first day of the calendar month (UTC) `now` (default: now) is in."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return date(now.year, now.month, 1)


# The month a ledger entry written now falls in, as the store's clock has it (the ledger's `at`).
_MONTH_NOW = cast(func.date_trunc("month", func.timezone("UTC", func.now())), Date)


# --- Running totals -------------------------------------------------------------------------

_SPENT = "aios.budgets.spent"


def add(store: Session, person_id: int | None, cost: float | None) -> None:
    """Add a ledger entry's `cost` to this month's totals: the organisation's, and the
    person's it's charged to (if any); in the caller's transaction, the entry's. Once that
    commits, the budget's thresholds are checked (`check_thresholds`)."""
    if not cost:
        return
    people = [ORGANISATION] if person_id is None else [ORGANISATION, person_id]
    statement = insert(SpendTotal).values(
        [{"month": _MONTH_NOW, "person_id": p, "cost": cost} for p in people]
    )
    store.execute(
        statement.on_conflict_do_update(
            index_elements=[SpendTotal.month, SpendTotal.person_id],
            set_={
                "cost": SpendTotal.cost + statement.excluded.cost,
                "updated_at": func.now(),
            },
        )
    )
    store.info[_SPENT] = True


@sa_event.listens_for(Session, "after_commit")
def _after_spending(session: Session) -> None:
    if not session.info.pop(_SPENT, None):
        return
    try:
        check_thresholds(store_id_of(session))
    except Exception:
        log.exception("couldn't check the budget's thresholds")


@sa_event.listens_for(Session, "after_rollback")
def _forget_spending(session: Session) -> None:
    session.info.pop(_SPENT, None)


def spent(store: Session, person_ids: list[int], month: date | None = None) -> dict[int, float]:
    """This month's (or `month`'s) totals of these people (`ORGANISATION` for the
    organisation's), by person; 0 for one with none."""
    month = month or this_month()
    found = dict(
        store.execute(
            select(SpendTotal.person_id, SpendTotal.cost).where(
                SpendTotal.month == month, SpendTotal.person_id.in_(person_ids)
            )
        ).all()
    )
    return {p: float(found.get(p) or 0) for p in person_ids}


# --- Budget and allowances ------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """The organisation's budget and a person's allowance in force (None: none)."""

    budget: float | None
    allowance: float | None
    name: str | None = None


def allowance_in_force(org: Organisation, member: OrganisationMember) -> float | None:
    """A member's own allowance, else their role's, else none."""
    if member.allowance is not None:
        return member.allowance
    value = (org.role_allowances or {}).get(member.role.value)
    return None if value is None else float(value)


def limits(db: Session, organisation_id: int, person_id: int | None) -> Limits:
    org = db.get(Organisation, organisation_id)
    if org is None:
        return Limits(None, None)
    allowance = name = None
    if person_id is not None:
        member = db.get(OrganisationMember, (organisation_id, person_id))
        if member is not None:
            allowance = allowance_in_force(org, member)
            if allowance is not None:
                user = db.get(User, person_id)
                name = user.full_name if user is not None else None
    return Limits(org.budget, allowance, name)


def refusal(store: Session, organisation_id: int, person_id: int | None) -> str | None:
    """Why new work charged to `person_id` (None: to nobody, the organisation only) is refused,
    if it is: the organisation at its budget, or the person at their allowance, this month."""
    with SessionLocal() as db:
        found = limits(db, organisation_id, person_id)
    if found.budget is None and found.allowance is None:
        return None
    people = [ORGANISATION] if found.allowance is None else [ORGANISATION, person_id]
    totals = spent(store, people)
    if found.budget is not None and totals[ORGANISATION] >= found.budget:
        return BUDGET_REACHED.format(amount=pounds(found.budget))
    if found.allowance is not None and totals[person_id] >= found.allowance:
        return ALLOWANCE_REACHED.format(name=found.name or SOMEONE, amount=pounds(found.allowance))
    return None


# --- Attribution ----------------------------------------------------------------------------


def root_person(
    store: Session,
    *,
    requested_by: int | None = None,
    parent: Invocation | None = None,
    started_by: Invocation | None = None,
    event: Event | None = None,
) -> int | None:
    """Who a new invocation is charged to: its chain of causes' root person. A workflow node's
    or a `start_workflow` call's is its parent's or caller's; an event's, that of the
    invocation whose run caused it, else a chat message's poster; else whoever asked for it."""
    if parent is not None:
        return parent.charged_to
    if started_by is not None:
        return started_by.charged_to
    if event is not None:
        cause_id = event.invocation_id or (event.chain[-1] if event.chain else None)
        if cause_id is not None:
            return store.scalar(select(Invocation.charged_to).where(Invocation.id == cause_id))
        payload = event.payload or {}
        if event.type == EventType.chat_message and payload.get("author_kind") == "user":
            author = payload.get("author_id")
            return author if isinstance(author, int) else None
        return None
    return requested_by


# --- Warnings -------------------------------------------------------------------------------


def _organisation(data_store_id: uuid.UUID) -> tuple[int, float | None] | None:
    with SessionLocal() as db:
        row = db.execute(
            select(Organisation.id, Organisation.budget)
            .join(DataStore, DataStore.owner_organisation_id == Organisation.id)
            .where(DataStore.id == data_store_id)
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def check_thresholds(data_store_id: uuid.UUID) -> list[str]:
    """Warn every admin once a month when the organisation's spending passes the warning
    threshold of its budget, and tell them once when it reaches it; the kinds notified. Nothing
    with no budget."""
    found = _organisation(data_store_id)
    if found is None or found[1] is None:
        return []
    organisation_id, budget = found
    month = this_month()
    kinds = []
    with store_session(data_store_id) as store:
        total = spent(store, [ORGANISATION], month)[ORGANISATION]
        for column, kind, at in (
            (
                SpendTotal.warned_at,
                notifications.BUDGET_WARNING,
                get_settings().budget_warning_threshold,
            ),
            (SpendTotal.reached_at, notifications.BUDGET_REACHED, 1.0),
        ):
            if total < budget * at:
                continue
            marked = store.scalar(
                update(SpendTotal)
                .where(
                    SpendTotal.month == month,
                    SpendTotal.person_id == ORGANISATION,
                    column.is_(None),
                )
                .values({column.key: func.now()})
                .returning(SpendTotal.person_id)
            )
            if marked is not None:
                kinds.append(kind)
        store.commit()
    if kinds:
        ref = {"month": month.strftime("%Y-%m")}
        with SessionLocal() as db:
            admins = notifications.admins(db, organisation_id)
            for kind in kinds:
                notifications.notify(db, admins, organisation_id, kind, ref)
            db.commit()
    return kinds


# --- Reconciling ----------------------------------------------------------------------------


def reconcile(data_store_id: uuid.UUID, month: date | None = None) -> dict[int, float]:
    """Put the store's totals for `month` (default: this one) right against the ledger; the
    drift found, by person (`ORGANISATION` for the organisation's), each logged."""
    month = month or this_month()
    next_month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    at = func.timezone("UTC", UsageEntry.at)
    in_month = (
        at >= month,
        at < next_month,
        UsageEntry.cost.is_not(None),
    )
    with store_session(data_store_id) as store:
        # Each total locked while it's compared, so spending recorded meanwhile waits.
        kept = dict(
            store.execute(
                select(SpendTotal.person_id, SpendTotal.cost)
                .where(SpendTotal.month == month)
                .with_for_update()
            ).all()
        )
        ledger = {
            person: float(cost)
            for person, cost in store.execute(
                select(UsageEntry.person_id, func.sum(UsageEntry.cost))
                .where(*in_month, UsageEntry.person_id.is_not(None))
                .group_by(UsageEntry.person_id)
            ).all()
        }
        ledger[ORGANISATION] = float(
            store.scalar(select(func.coalesce(func.sum(UsageEntry.cost), 0)).where(*in_month))
        )
        drift: dict[int, float] = {}
        for person in set(kept) | set(ledger):
            want, have = ledger.get(person, 0.0), float(kept.get(person) or 0)
            if abs(want - have) < 1e-9:
                continue
            drift[person] = want - have
            store.execute(
                insert(SpendTotal)
                .values(month=month, person_id=person, cost=want)
                .on_conflict_do_update(
                    index_elements=[SpendTotal.month, SpendTotal.person_id],
                    set_={"cost": want, "updated_at": func.now()},
                )
            )
        store.commit()
    for person, amount in drift.items():
        log.warning(
            "spend total drifted in store %s for %s, month %s: off by £%.6f, put right",
            data_store_id,
            "the organisation" if person == ORGANISATION else f"person {person}",
            month,
            amount,
        )
    return drift


def reconcile_all(now: datetime | None = None) -> None:
    """Reconcile every ready store's totals for this month, and last month's on its first day
    (spending recorded just before midnight). A store that can't be reached waits for tomorrow."""
    month = this_month(now)
    months = [month]
    if (now or datetime.now(UTC)).astimezone(UTC).day == 1:
        months.append(date(month.year - (month.month == 1), (month.month - 2) % 12 + 1, 1))
    for store_id, _ in ready_stores():
        for m in months:
            try:
                reconcile(store_id, m)
            except Exception:
                log.exception("couldn't reconcile the spend totals of data store %s", store_id)


# --- Reading --------------------------------------------------------------------------------


def overview(
    db: Session, store: Session, org: Organisation, viewer: int, everyone: bool
) -> dict[str, Any]:
    """The budget, this month's spend against it and the role allowances; with each member's
    allowance (theirs, and in force) and spend: every member's for `everyone`, else only the
    viewer's."""
    members = db.scalars(
        select(OrganisationMember)
        .where(OrganisationMember.organisation_id == org.id)
        .order_by(OrganisationMember.joined_at)
    ).all()
    if not everyone:
        members = [m for m in members if m.user_id == viewer]
    totals = spent(store, [ORGANISATION, *(m.user_id for m in members)])
    return {
        "month": this_month().strftime("%Y-%m"),
        "budget": org.budget,
        "spent": totals[ORGANISATION],
        "warning_threshold": get_settings().budget_warning_threshold,
        "role_allowances": {
            role.value: (org.role_allowances or {}).get(role.value) for role in Role
        },
        "members": [
            {
                "user_id": m.user_id,
                "role": m.role,
                "allowance": m.allowance,
                "allowance_in_force": allowance_in_force(org, m),
                "spent": totals[m.user_id],
            }
            for m in members
        ],
    }
