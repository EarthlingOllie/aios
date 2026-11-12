"""The audit log: who did what, to what, and when, in each organisation's store
(`aios.audit_log`, `aios.datastores.models.AuditEntry`).

It's append-only: the platform's read-write role may only insert and read it, and a trigger
refuses updates, deletes and truncation to anyone, the store's owner included. Its details are on
the deny list, so the data tools never read them; agents read the reporting view
(`reporting.audit_log`) of who did what to what, and when.

Two ways in:

- **Store-side actions** (configuration changes, archiving, approval decisions, approval rules,
  `write_data` calls) `record` an entry in the store session they act in, so it commits, or
  doesn't, with what it records.
- **Platform-side actions** (roles, memberships, invitations, renames, settings, deletion
  scheduling) `enqueue` one in the platform's outbox (`aios.models.AuditOutbox`), in the
  platform transaction that acts. `drain` delivers the outbox into each store, every
  `audit_drain_seconds` on the routine worker's beat, idempotently by outbox id (the
  store's `outbox_id` is unique): a store that's briefly unavailable gets its entries once it's
  back, each exactly once.

Actions are dotted, `<what>.<what happened>`: `agent.archived`, `approval.decided`,
`member.role_changed`, `data.written`.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.datastores.models import AuditEntry
from aios.db import SessionLocal
from aios.models import AuditActorKind, AuditOutbox, DataStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Actor:
    kind: AuditActorKind
    user_id: int | None = None
    agent_id: int | None = None
    run_id: int | None = None


def person(user_id: int) -> Actor:
    return Actor(AuditActorKind.person, user_id=user_id)


def agent(agent_id: int | None, run_id: int | None) -> Actor:
    return Actor(AuditActorKind.agent, agent_id=agent_id, run_id=run_id)


SYSTEM = Actor(AuditActorKind.system)


def _columns(
    actor: Actor, action: str, target_kind: str | None, target_id: Any, details: dict | None
) -> dict[str, Any]:
    return {
        "actor_kind": actor.kind,
        "actor_user_id": actor.user_id,
        "actor_agent_id": actor.agent_id,
        "actor_run_id": actor.run_id,
        "action": action,
        "target_kind": target_kind,
        "target_id": None if target_id is None else str(target_id),
        "details": details or {},
    }


def record(
    store: Session,
    actor: Actor,
    action: str,
    target_kind: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
    *,
    at: datetime | None = None,
) -> None:
    """Add an entry to the store's audit log, in the caller's store transaction (at `at`, else
    as the transaction's time)."""
    entry = AuditEntry(**_columns(actor, action, target_kind, target_id, details))
    if at is not None:
        entry.at = at
    store.add(entry)


def configured(
    store: Session, user_id: int, kind: str, thing: Any, what: str, **details: Any
) -> None:
    """Record that a person `what`-ed (created, updated, archived, ...) one of the store's
    agents, skills, workflows, schedules or sources (`thing`, of `kind`), by its id and name,
    in the caller's store transaction (flushed first, so a new one has its id)."""
    store.flush()
    name = getattr(thing, "name", None)
    record(
        store,
        person(user_id),
        f"{kind}.{what}",
        kind,
        thing.id,
        ({"name": name} if name is not None else {}) | details,
    )


def enqueue(
    db: Session,
    organisation_id: int,
    actor: Actor,
    action: str,
    target_kind: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an entry for the organisation's audit log to the platform's outbox, in the caller's
    platform transaction; `drain` delivers it to the organisation's store."""
    db.add(
        AuditOutbox(
            organisation_id=organisation_id,
            **_columns(actor, action, target_kind, target_id, details),
        )
    )


# Outbox rows delivered per store at a time.
DRAIN_BATCH = 500


def drain() -> int:
    """Deliver the platform's outbox into each organisation's store: insert each row there (by
    its outbox id, so one delivered before, whose outbox row wasn't deleted, isn't recorded
    twice), commit the store, then delete the rows from the outbox. A store that can't be
    reached keeps its rows for the next drain (an organisation's go with it, its store too).
    Answers how many entries were delivered."""
    from aios.datastores import store_session

    delivered = 0
    with SessionLocal() as db:
        organisation_ids = db.scalars(select(AuditOutbox.organisation_id).distinct()).all()
    for organisation_id in organisation_ids:
        with SessionLocal() as db:
            # Locked, so two drains at once deliver each row from one of them.
            rows = db.scalars(
                select(AuditOutbox)
                .where(AuditOutbox.organisation_id == organisation_id)
                .order_by(AuditOutbox.id)
                .limit(DRAIN_BATCH)
                .with_for_update(skip_locked=True)
            ).all()
            if not rows:
                continue
            store_id = db.scalar(
                select(DataStore.id).where(DataStore.owner_organisation_id == organisation_id)
            )
            if store_id is None:
                # Not made yet: they wait for it.
                continue
            try:
                with store_session(store_id) as store:
                    store.execute(
                        insert(AuditEntry)
                        .values([_delivered(r) for r in rows])
                        .on_conflict_do_nothing(index_elements=[AuditEntry.outbox_id])
                    )
                    store.commit()
            except Exception:
                # Not ready, busy, down: its rows wait for the next drain.
                log.warning(
                    "Couldn't deliver audit entries to data store %s", store_id, exc_info=True
                )
                continue
            db.execute(delete(AuditOutbox).where(AuditOutbox.id.in_([r.id for r in rows])))
            db.commit()
            delivered += len(rows)
    return delivered


def _delivered(row: AuditOutbox) -> dict[str, Any]:
    return {
        "at": row.at,
        "actor_kind": row.actor_kind,
        "actor_user_id": row.actor_user_id,
        "actor_agent_id": row.actor_agent_id,
        "actor_run_id": row.actor_run_id,
        "action": row.action,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "details": row.details,
        "outbox_id": row.id,
    }


# --- Reading -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    # A person, by user id.
    actor_user_id: int | None = None
    # An agent, by its id in the store.
    actor_agent_id: int | None = None
    # An action, or a family of them: `agent` matches `agent.archived` and `agent.created`.
    action: str | None = None
    # On or after this day (UTC).
    since: date | None = None
    # On or before this day (UTC).
    until: date | None = None


def cursor_of(entry: AuditEntry) -> str:
    return f"{entry.at.isoformat()}_{entry.id}"


def _parse_cursor(cursor: str) -> tuple[datetime, int]:
    at, _, entry_id = cursor.rpartition("_")
    return datetime.fromisoformat(at), int(entry_id)


def entries(
    store: Session, filters: Filters, *, before: str | None = None, limit: int = 50
) -> tuple[list[AuditEntry], str | None]:
    """The store's audit entries matching `filters`, newest first, `limit` at a time: those
    after the cursor `before` (the previous page's `next`), and the cursor of the next page
    (None on the last). Raises ValueError for a cursor that isn't one."""
    query = select(AuditEntry)
    if filters.actor_user_id is not None:
        query = query.where(AuditEntry.actor_user_id == filters.actor_user_id)
    if filters.actor_agent_id is not None:
        query = query.where(AuditEntry.actor_agent_id == filters.actor_agent_id)
    if filters.action:
        action = filters.action.strip().rstrip(".")
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(
            or_(AuditEntry.action == action, AuditEntry.action.like(f"{escaped}.%"))
        )
    if filters.since is not None:
        query = query.where(AuditEntry.at >= _midnight(filters.since))
    if filters.until is not None:
        query = query.where(AuditEntry.at < _midnight(filters.until + timedelta(days=1)))
    if before is not None:
        at, entry_id = _parse_cursor(before)
        query = query.where(tuple_(AuditEntry.at, AuditEntry.id) < tuple_(at, entry_id))
    found = store.scalars(
        query.order_by(AuditEntry.at.desc(), AuditEntry.id.desc()).limit(limit + 1)
    ).all()
    page = list(found[:limit])
    return page, cursor_of(page[-1]) if len(found) > limit else None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
