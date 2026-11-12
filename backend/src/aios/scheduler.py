"""Fires schedule triggers (`aios.datastores.models.Trigger`, in each organisation's store), on
the run service (`aios.run_service`).

Every run service process ticks every `scheduler_interval_seconds` (`tick`), unless
`scheduler_enabled` is off. Each tick claims the schedules that are due (`claim_due`) and fires
each (`fire`): it starts the schedule's run and its invocation in the store, and enqueues the
agent's turn on the run service's background lane, which runs it and records how it went on the
trigger (`aios.run_service.work`).

- What's due is found in the platform's schedule due index (`aios.models.ScheduleDue`,
  `aios.schedules`), one row per enabled schedule of every store with when it's next due. The
  store stays the source of truth: claiming re-reads each due schedule there.
- Claiming locks the due index rows with SKIP LOCKED, so however many processes poll, each row
  is looked at by one. For each, the trigger is locked in its store and read again: one that's
  gone or disabled leaves the index, one not due yet (the index was out of date) only moves its
  row, and one that's due is moved on to its next run in the store, which commits first, then
  its row with it. So each run of a schedule fires exactly once: a row whose update was lost is
  put right the next time it's due, or by the repair sweep, and never fires twice, as the store
  has moved on.
- Missed runs aren't caught up: a schedule that's overdue (no process was up) fires once, and its
  next run is worked out from now.
- Firing an agent's schedule starts a new run of the agent's current version, with its
  invocation (cause schedule), and enqueues its turn, with the trigger's `input` as the user's
  message. The outcome goes on the trigger (`last_status`, `last_error`, `last_run_id`), unless
  the schedule has fired again since.
- Firing a workflow's schedule starts a run of the workflow's current version, with the
  trigger's `input` as the run's input, exactly as `POST /api/workflows/{id}/runs` does
  (`aios.api.workflows.new_workflow_run`), and enqueues its first advance on the run service.
  The trigger records the run (`last_workflow_run_id`) and, once it's queued, that it fired;
  from then on it shows how the run is going (`aios.api.triggers.last_outcome`). One that can't
  start (no nodes, an agent that can't run, the queue down) records that it failed, without a
  run.
- A schedule's run is started by no member (its `created_by` is null): it executes for its
  agent's or workflow's organisation, over the organisation's store, so it goes on working
  whoever made the schedule or its target, and whether or not they're still a member.
- Editing a schedule (pausing it, changing its target or message, ...) after it's claimed but
  before its run has started drops that firing: it was for the old settings
  (`aios.api.triggers.cancel_unstarted_firing`). Firing re-reads the trigger under its row lock
  (`_claimed`) and sees that. Once the run has started, it finishes.
- A process that dies between claiming a schedule and starting its run can't record that. So a
  schedule is stored as running from its claim (`claimed_at` set then), and each tick fails in
  every store those claimed `STALE_AFTER` ago that never started a run (`fail_stalled`). Once
  its run has started, its turn is the run service's: retried if its process dies, and failed
  after a few attempts (`aios.run_service.work`), which the trigger records.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aios import cron
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    ready_stores,
    store_id_of,
    store_session,
)
from aios.datastores.models import Agent, Invocation, Trigger, Workflow
from aios.db import SessionLocal
from aios.models import DataStore, ScheduleDue, TriggerStatus, TriggerTargetKind
from aios.run_service import queue
from aios.run_service.errors import STOPPED, CantStart
from aios.run_service.work import Schedule, turn_work

log = logging.getLogger(__name__)

# How long a schedule may stay claimed without starting its run before its process counts as
# gone.
STALE_AFTER = timedelta(seconds=90)

# Most schedules claimed per tick, however many slots are free; any left over are due on the next.
BATCH = 50

# Why a firing is failed if it was dropped (see `_claimed`) without its schedule being edited.
CHANGED = "The schedule was changed as it fired; it runs as changed next time."

# A schedule claimed to fire: its store, and its id there.
Claim = tuple[uuid.UUID, int]


# What claiming one due schedule found: whether it fired, and when it's next due (None: it's gone
# or disabled), or `LOCKED` (being edited, or a firing of it is starting its run).
LOCKED = object()


def _claim_one(store: Session, trigger_id: int, now: datetime):
    """Claim the store's trigger if it's due at `now`, under its row lock, moving it on to its
    next run: (whether it was claimed, when it's next due), or `LOCKED` if another holds its
    lock (an edit, or a firing starting its run: it's left due, for the next tick). Commits."""
    trigger = store.scalars(
        select(Trigger)
        .where(Trigger.id == trigger_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if trigger is None:
        gone = store.scalar(select(Trigger.id).where(Trigger.id == trigger_id)) is None
        store.rollback()
        return (False, None) if gone else LOCKED
    if not trigger.enabled or trigger.next_run_at is None:
        store.rollback()
        return False, None
    if trigger.next_run_at > now:
        # The index was out of date: it moves with the trigger.
        store.rollback()
        return False, trigger.next_run_at
    trigger.last_fired_at = now
    # This firing's run, once it has one; until then the trigger shows it running.
    trigger.last_run_id = None
    trigger.last_workflow_run_id = None
    try:
        trigger.next_run_at = cron.next_run(trigger.cron, trigger.timezone, now)
    except cron.CronError as exc:
        # Valid when saved, so only if, say, its timezone has left the tz database.
        trigger.enabled = False
        trigger.next_run_at = None
        trigger.last_status = TriggerStatus.failed
        trigger.last_error = f"{exc}. The schedule has been turned off."
        store.commit()
        return False, None
    trigger.last_status = TriggerStatus.running
    trigger.last_error = None
    trigger.claimed_at = func.now()
    store.commit()
    return True, trigger.next_run_at


def claim_due(db: Session, now: datetime, limit: int = BATCH) -> list[Claim]:
    """Claim up to `limit` of the schedules due at `now`, the most overdue first: move each on to
    its next run in its store, and its due index row with it; return their stores and ids.
    Commits `db`."""
    due = db.scalars(
        select(ScheduleDue)
        .where(ScheduleDue.next_run_at <= now)
        .order_by(ScheduleDue.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed = []
    for row in due:
        try:
            with store_session(row.data_store_id) as store:
                found = _claim_one(store, row.trigger_id, now)
        except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy) as exc:
            # Left due: tried again next tick.
            log.warning(
                "couldn't claim schedule %s of data store %s: %s",
                row.trigger_id,
                row.data_store_id,
                exc,
            )
            continue
        if found is LOCKED:
            continue
        fired, next_run_at = found
        if fired:
            claimed.append((row.data_store_id, row.trigger_id))
        if next_run_at is None:
            db.delete(row)
        else:
            row.next_run_at = next_run_at
    db.commit()
    return claimed


def fail_stalled(store: Session) -> None:
    """Fail the store's schedules claimed long ago that never started their run: the process
    firing them has gone (see the module docstring). Commits."""
    store.execute(
        update(Trigger)
        .where(
            Trigger.last_status == TriggerStatus.running,
            Trigger.last_run_id.is_(None),
            Trigger.last_workflow_run_id.is_(None),
            or_(Trigger.claimed_at.is_(None), Trigger.claimed_at < func.now() - STALE_AFTER),
        )
        .values(last_status=TriggerStatus.failed, last_error=STOPPED)
    )
    store.commit()


def _sweep() -> None:
    """In every ready store, the schedules whose process has gone before starting their run. A
    store that can't be reached is left for the next tick."""
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                fail_stalled(store)
        except Exception:
            log.exception("couldn't sweep data store %s", store_id)


def _claim(now: datetime, limit: int) -> list[Claim]:
    with SessionLocal() as db:
        return claim_due(db, now, limit)


class _Gone(Exception):
    """The trigger has gone, fired again, or changed target since it was claimed."""


def _claimed(
    store: Session,
    trigger_id: int,
    fired_at: datetime,
    kind: TriggerTargetKind | None = None,
    *,
    lock: bool = False,
) -> Trigger:
    """The store's trigger, still claimed by this firing (and targeting `kind`, if given); else
    `_Gone`.

    Editing a trigger (pausing it, say) drops a firing that hasn't started its run
    (`aios.api.triggers.cancel_unstarted_firing`), which this sees as it having fired again.
    With `lock`, the row stays locked until commit, so the run and the trigger's link to it are
    committed only if the firing is still claimed, and an edit racing the start waits and then
    sees the run. Take it after the target's lock (`new_agent_run`, `new_workflow_run`): an
    edit locks the target first, then the trigger.
    """
    query = select(Trigger).where(Trigger.id == trigger_id)
    if lock:
        query = query.with_for_update()
    trigger = store.scalars(query.execution_options(populate_existing=True)).one_or_none()
    # Changed target: edited since it was claimed, so it fires the new one next time.
    if (
        trigger is None
        or trigger.last_fired_at != fired_at
        or (kind is not None and trigger.target_kind != kind)
    ):
        raise _Gone
    return trigger


def _organisation(data_store_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
    if organisation_id is None:
        raise CantStart("The organisation has been deleted.")
    return organisation_id


@dataclass(frozen=True)
class _Started:
    """An agent schedule's run, started: its organisation, and its invocation."""

    organisation_id: int
    invocation_id: int


def _start_agent_run(store: Session, trigger: Trigger, fired_at: datetime) -> _Started:
    """Start a run for an agent's schedule, with its invocation; commits."""
    data_store_id = store_id_of(store)
    organisation_id = _organisation(data_store_id)
    agent = store.get(Agent, trigger.agent_id)
    if agent is None:
        raise CantStart("The agent has been deleted.")
    try:
        run = agents_api.new_agent_run(
            store, organisation_id, agent, created_by=None, trigger_id=trigger.id
        )
    except HTTPException as exc:
        raise CantStart(exc.detail) from exc
    trigger = _claimed(store, trigger.id, fired_at, TriggerTargetKind.agent, lock=True)
    # The run and the trigger's link to it, under the trigger's lock taken after the agent's.
    trigger.last_run_id = run.id
    invocation_id = store.scalar(select(Invocation.id).where(Invocation.agent_run_id == run.id))
    store.commit()
    return _Started(organisation_id, invocation_id)


def _start_workflow_run(store: Session, trigger: Trigger, fired_at: datetime) -> queue.Work:
    """Start a run for a workflow's schedule, and enqueue its first advance (`aios.workflows`);
    the item, to wake the run service for.

    Raises `HTTPException` as starting one by hand would, if it can't start or be queued."""
    organisation_id = _organisation(store_id_of(store))
    workflow = store.get(Workflow, trigger.workflow_id)
    if workflow is None:
        raise CantStart("The workflow has been deleted.")
    run = workflows_api.new_workflow_run(
        store, organisation_id, workflow, trigger.input, created_by=None, trigger_id=trigger.id
    )
    trigger = _claimed(store, trigger.id, fired_at, TriggerTargetKind.workflow, lock=True)
    trigger.last_workflow_run_id = run.id
    # Commits the run and the trigger's link to it, and queues it; if queueing fails, the run
    # is deleted, and the link with it.
    return workflows_api.queue_workflow_run(store, run, organisation_id)


def _start(data_store_id: uuid.UUID, trigger_id: int, fired_at: datetime) -> queue.Work | None:
    """Start the claimed schedule's run: the agent's turn to enqueue, or None for a workflow (its
    first advance enqueued, so the trigger shows how the run goes from here) or a trigger that's
    gone or fired again. Raises `CantStart` to record why it couldn't, or
    `HTTPException` as starting a run by hand would."""
    with store_session(data_store_id) as store:
        try:
            trigger = _claimed(store, trigger_id, fired_at)
        except _Gone:
            return None
        message = trigger.input
        try:
            if trigger.target_kind == TriggerTargetKind.workflow:
                advance = _start_workflow_run(store, trigger, fired_at)
                _record(data_store_id, trigger_id, fired_at, None)
                queue.wake(advance.lane)
                return None
            started = _start_agent_run(store, trigger, fired_at)
        except _Gone as exc:
            # A no-op to record: it's gone, fired again, or was edited, which drops the firing
            # (see `_claimed`). Recorded anyway, so one dropped some other way isn't left
            # running.
            raise CantStart(CHANGED) from exc
    return turn_work(
        started.organisation_id,
        data_store_id,
        started.invocation_id,
        Schedule(trigger_id, fired_at, message),
    )


def _record(data_store_id: uuid.UUID, trigger_id: int, fired_at: datetime, error: str | None):
    """Record how the firing went; a no-op if the trigger was deleted, or has fired again."""
    with store_session(data_store_id) as store:
        store.execute(
            update(Trigger)
            .where(Trigger.id == trigger_id, Trigger.last_fired_at == fired_at)
            .values(
                last_status=TriggerStatus.failed if error else TriggerStatus.succeeded,
                last_error=error,
            )
        )
        store.commit()


def _fire(data_store_id: uuid.UUID, trigger_id: int, fired_at: datetime) -> queue.Work | None:
    error = None
    try:
        return _start(data_store_id, trigger_id, fired_at)
    except CantStart as exc:
        error = str(exc)
    except HTTPException as exc:
        error = str(exc.detail)
    except Exception:
        log.exception("schedule %s of data store %s couldn't fire", trigger_id, data_store_id)
        error = "Couldn't start the run."
    _record(data_store_id, trigger_id, fired_at, error)
    return None


def _enqueue(works: list[queue.Work]) -> None:
    if not works:
        return
    with SessionLocal() as db:
        queue.enqueue(db, works)
        db.commit()
    queue.wake(*{w.lane for w in works})


async def fire(data_store_id: uuid.UUID, trigger_id: int, fired_at: datetime) -> None:
    """Fire one claimed trigger: start its run, and enqueue its agent's turn; or record why it
    couldn't start."""
    work = await run_in_threadpool(_fire, data_store_id, trigger_id, fired_at)
    await run_in_threadpool(_enqueue, [work] if work else [])


async def tick(now: datetime | None = None) -> list[Claim]:
    """Fail what a dead process left claimed, claim what's due, and fire each; returns the
    claimed (store, trigger id)s."""
    now = now or datetime.now(UTC)
    await run_in_threadpool(_sweep)
    claimed = await run_in_threadpool(_claim, now, BATCH)
    works = []
    for store_id, trigger_id in claimed:
        if work := await run_in_threadpool(_fire, store_id, trigger_id, now):
            works.append(work)
    await run_in_threadpool(_enqueue, works)
    return claimed
