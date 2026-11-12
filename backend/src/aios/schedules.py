"""The schedule due index: the platform's thin copy of when each enabled schedule is next due
(`aios.models.ScheduleDue`), so the scheduler can find what's due across every organisation's
store with one indexed query (`aios.scheduler.claim_due`).

Schedules themselves (`aios.datastores.models.Trigger`) are in the stores, which are the source
of truth. The index is a derived copy, written after the store commits and idempotently:

- **Saving a schedule** (creating, editing, pausing, archiving its target, deleting it) writes its
  row (`index`): its next run time, or no row while it's disabled.
- **Claiming one** moves its row on with the trigger, in the claim's own transaction
  (`aios.scheduler.claim_due`), which re-reads the trigger in the store under its row lock and
  acts on that: a row that's out of date only makes the scheduler look at the trigger early.
- **The repair sweep** (`repair`, on the routine worker's beat, with the people mirror's)
  rebuilds every ready store's rows from its triggers, and reports what it fixed: rows it added
  (a lost one), updated (a stale one) and removed (a schedule disabled or gone).
"""

import logging
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.datastores import connections
from aios.datastores.models import Trigger
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus, ScheduleDue

log = logging.getLogger(__name__)


def due_at(trigger: Trigger) -> datetime | None:
    """When the trigger is next due, as its index row has it: None (no row) while it's
    disabled."""
    return trigger.next_run_at if trigger.enabled else None


def write(
    db: Session,
    data_store_id: uuid.UUID,
    organisation_id: int,
    trigger_id: int,
    next_run_at: datetime | None,
) -> None:
    """Set the store's trigger's row: when it's next due, or none. In the caller's
    transaction."""
    if next_run_at is None:
        db.execute(
            delete(ScheduleDue).where(
                ScheduleDue.data_store_id == data_store_id, ScheduleDue.trigger_id == trigger_id
            )
        )
        return
    db.execute(
        insert(ScheduleDue)
        .values(
            data_store_id=data_store_id,
            trigger_id=trigger_id,
            organisation_id=organisation_id,
            next_run_at=next_run_at,
        )
        .on_conflict_do_update(
            index_elements=[ScheduleDue.data_store_id, ScheduleDue.trigger_id],
            set_={"next_run_at": next_run_at},
        )
    )


def index(data_store_id: uuid.UUID, triggers: Iterable[tuple[int, datetime | None]]) -> None:
    """Index these (trigger id, next due or None) of the store, once the store has committed
    them. A write that fails is logged, and left to the repair sweep: the store has the
    schedules either way."""
    triggers = list(triggers)
    if not triggers:
        return
    try:
        with SessionLocal() as db:
            organisation_id = db.scalar(
                select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
            )
            if organisation_id is None:
                return
            for trigger_id, next_run_at in triggers:
                write(db, data_store_id, organisation_id, trigger_id, next_run_at)
            db.commit()
    except Exception:
        log.exception("Couldn't index the schedules of data store %s", data_store_id)


@dataclass
class Fixed:
    """What rebuilding a store's rows changed, by trigger id: rows added (missing), updated
    (stale) and removed (a schedule disabled or deleted)."""

    added: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def rebuild(data_store_id: uuid.UUID, organisation_id: int) -> Fixed:
    """Bring the store's rows to match its triggers now; returns what it changed."""
    with connections.store_session(data_store_id, "ro") as store:
        due = dict(
            store.execute(
                select(Trigger.id, Trigger.next_run_at).where(
                    Trigger.enabled, Trigger.next_run_at.is_not(None)
                )
            ).all()
        )
    fixed = Fixed()
    with SessionLocal() as db:
        indexed = dict(
            db.execute(
                select(ScheduleDue.trigger_id, ScheduleDue.next_run_at)
                .where(ScheduleDue.data_store_id == data_store_id)
                .with_for_update()
            ).all()
        )
        for trigger_id, next_run_at in due.items():
            if trigger_id not in indexed:
                fixed.added.append(trigger_id)
            elif indexed[trigger_id] != next_run_at:
                fixed.updated.append(trigger_id)
            else:
                continue
            write(db, data_store_id, organisation_id, trigger_id, next_run_at)
        for trigger_id in indexed.keys() - due.keys():
            fixed.removed.append(trigger_id)
            write(db, data_store_id, organisation_id, trigger_id, None)
        db.commit()
    return fixed


def repair(store_ids: Collection[uuid.UUID] | None = None) -> dict[uuid.UUID, Fixed]:
    """The repair sweep's part for schedules: rebuild every ready store's rows (or only
    `store_ids`') from its triggers, one store at a time; returns, and logs, what it fixed in
    each store it fixed something in. A store that fails is logged and left to the next sweep."""
    with SessionLocal() as db:
        query = select(DataStore.id, DataStore.owner_organisation_id).where(
            DataStore.status == DataStoreStatus.ready,
            DataStore.owner_organisation_id.is_not(None),
        )
        if store_ids is not None:
            query = query.where(DataStore.id.in_(store_ids))
        stores = db.execute(query.order_by(DataStore.created_at)).all()
    report: dict[uuid.UUID, Fixed] = {}
    for store_id, organisation_id in stores:
        try:
            fixed = rebuild(store_id, organisation_id)
        except Exception:
            log.exception("Repairing data store %s's schedule due index failed", store_id)
            continue
        if fixed:
            log.warning(
                "Repaired data store %s's schedule due index: added %s, updated %s, removed %s",
                store_id,
                fixed.added,
                fixed.updated,
                fixed.removed,
            )
            report[store_id] = fixed
    return report
