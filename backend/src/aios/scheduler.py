"""Fires schedule triggers (`aios.datastores.models.Trigger`, in each organisation's store).

Each API process runs `run_forever` alongside serving requests. Every tick it claims the
schedules that are due (`claim_due`) and fires each in a task of its own (`fire`), through the
firing machinery every cause of a run outside a request shares (`aios.firing`: the cap, the
heartbeat, the stall sweep, and starting a run and running one turn of it). What's a schedule's
own is `ScheduleFiring`: what it claims, and where its heartbeat and outcome live.

- What's due is found in the platform's schedule due index (`aios.models.ScheduleDue`,
  `aios.schedules`), one row per enabled schedule of every store with when it's next due. The
  store stays the source of truth: claiming re-reads each due schedule there.
- A process fires at most `scheduler_max_concurrent_runs` at once, and claims no more than it has
  free slots for: claiming moves a schedule on, so one claimed but left waiting would be late or,
  if the process stopped, lost. What it leaves stays due, for another replica or a later tick.
  With the slots left, it also runs the turns of agents mentioned in chats that are waiting for
  one (`aios.api.chats.launch_turns`), and its sweep fails those whose process has gone.

- Claiming locks the due index rows with SKIP LOCKED, so however many replicas poll, each row is
  looked at by one. For each, the trigger is locked in its store and read again: one that's gone
  or disabled leaves the index, one not due yet (the index was out of date) only moves its row,
  and one that's due is moved on to its next run in the store, which commits first, then its row
  with it. A row whose update was lost is put right the next time it's due, or by the repair
  sweep, and never fires twice: the store has moved on.
- Missed runs aren't caught up: a schedule that's overdue (no process was up) fires once, and its
  next run is worked out from now.
- Firing an agent's schedule starts a new run of the agent's current version, with the trigger's
  `input` as the user's message, and runs one turn exactly as the chat endpoint does
  (`aios.api.agents.turn_stream`). The outcome goes on the trigger (`last_status`, `last_error`,
  `last_run_id`), unless the schedule has fired again since.
- Firing a workflow's schedule starts a run of the workflow's current version, with the
  trigger's `input` as the run's input, exactly as `POST /api/workflows/{id}/runs` does
  (`aios.api.workflows.new_workflow_run`), and queues it for the worker. The trigger records the
  run (`last_workflow_run_id`) and, once it's queued, that it fired; from then on it shows how the
  run is going (`aios.api.triggers.last_outcome`). One that can't start (no nodes, an agent
  that can't run, the queue down) records that it failed, without a run.
- A schedule's run is started by no member (its `created_by` is null): it executes for its
  agent's or workflow's organisation, over the organisation's store, so it goes on working
  whoever made the schedule or its target, and whether or not they're still a member.
- Editing a schedule (pausing it, changing its target or message, ...) after it's claimed but
  before its run has started drops that firing: it was for the old settings
  (`aios.api.triggers.cancel_unstarted_firing`). Firing re-reads the trigger under its row lock
  (`_claimed`) and sees that. Once the run has started, it finishes.
- A turn in flight when the process stops is stopped like a chat turn the client abandons: what
  the agent got to is kept, and the schedule records that it failed.
- A process that dies (killed, OOM, a deploy) can't record that, nor can a tick stopped between
  claiming and firing. So while a schedule is stored as running, the process firing it refreshes
  its heartbeat (`heartbeat_at`, set when it's claimed) every `HEARTBEAT_SECONDS`
  (`ScheduleFiring.beat`, with the run's own), and each tick, on any replica, fails in every store
  those whose heartbeat has stopped for `STALE_AFTER`, with their agent runs if those are stalled
  too (`fail_stalled`). A live replica keeps beating its own fires, so others leave them be. Only
  the stored status counts: a workflow's schedule is stored as running only until its run is
  queued, and the worker looks after the run from there (`aios.workflows`).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from aios import cron, firing
from aios.api import agents as agents_api
from aios.api import chats as chats_api
from aios.api import workflows as workflows_api
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    ready_stores,
    store_id_of,
    store_session,
)
from aios.datastores.models import Agent, Trigger, Workflow
from aios.db import SessionLocal
from aios.firing import HEARTBEAT_SECONDS, STALE_AFTER, STOPPED, CantStart
from aios.models import DataStore, ScheduleDue, TriggerStatus, TriggerTargetKind

log = logging.getLogger(__name__)

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
    trigger.heartbeat_at = func.now()
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
    """Fail the store's schedules stored as running whose heartbeat has stopped: the process
    firing them has gone (see the module docstring); and their agent runs, if still marked
    running with a stale heartbeat (`firing.fail_stalled`). Commits."""
    stalled = store.scalars(
        update(Trigger)
        .where(
            Trigger.last_status == TriggerStatus.running,
            or_(Trigger.heartbeat_at.is_(None), Trigger.heartbeat_at < func.now() - STALE_AFTER),
        )
        .values(last_status=TriggerStatus.failed, last_error=STOPPED)
        .returning(Trigger.last_run_id)
    ).all()
    firing.fail_stalled_in(store, [run_id for run_id in stalled if run_id is not None])
    store.commit()


def _sweep() -> None:
    """In every ready store: the schedules whose process has gone, and their runs (a schedule
    is running from when it's claimed, before it has a run); and the chat mentions' turns. A
    store that can't be reached is left for the next tick."""
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                fail_stalled(store)
                chats_api.fail_stopped_turns(store)
        except Exception:
            log.exception("couldn't sweep data store %s", store_id)


def _claim(now: datetime, limit: int) -> list[Claim]:
    with SessionLocal() as db:
        return claim_due(db, now, limit)


# Refreshes the heartbeats of the fires this process is running, schedules' among them; the
# loop that calls it every `HEARTBEAT_SECONDS` is started by `run_forever`.
beat = firing.beat


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


def _start_agent_run(store: Session, trigger: Trigger, fired_at: datetime) -> firing.Turn:
    """Start a run for an agent's schedule; commits."""
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
    store.commit()
    return firing.Turn(data_store_id, agent.id, run.id, trigger.input)


def _start_workflow_run(store: Session, trigger: Trigger, fired_at: datetime) -> None:
    """Start a run for a workflow's schedule, and queue it for the worker (`aios.workflows`).

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
    workflows_api.queue_workflow_run(store, run)


@dataclass(frozen=True)
class ScheduleFiring:
    """One firing of a schedule, as the cause of a run (`aios.firing.Cause`): the store's
    trigger as claimed at `fired_at`, which is what the firing acts on and records to."""

    data_store_id: uuid.UUID
    trigger_id: int
    fired_at: datetime

    async def prepare(self) -> None:
        # A schedule's run starts with its stored input: nothing to do first.
        pass

    def start(self) -> firing.Turn | None:
        """The agent's turn to run; None for a workflow (queued for the worker, so the trigger
        shows how the run goes from here) or a trigger that's gone or fired again."""
        with store_session(self.data_store_id) as store:
            try:
                trigger = _claimed(store, self.trigger_id, self.fired_at)
            except _Gone:
                return None
            try:
                if trigger.target_kind == TriggerTargetKind.workflow:
                    _start_workflow_run(store, trigger, self.fired_at)
                    return None
                return _start_agent_run(store, trigger, self.fired_at)
            except _Gone as exc:
                # A no-op to record: it's gone, fired again, or was edited, which drops the
                # firing (see `_claimed`). Recorded anyway, so one dropped some other way isn't
                # left running.
                raise CantStart(CHANGED) from exc

    def beat(self) -> None:
        # Not once it's recorded how it went, or fired again (on another replica, say).
        with store_session(self.data_store_id) as store:
            store.execute(
                update(Trigger)
                .where(
                    Trigger.id == self.trigger_id,
                    Trigger.last_fired_at == self.fired_at,
                    Trigger.last_status == TriggerStatus.running,
                )
                .values(heartbeat_at=func.now())
            )
            store.commit()

    def record(self, error: str | None) -> None:
        # A no-op if the trigger was deleted, or has fired again since.
        with store_session(self.data_store_id) as store:
            store.execute(
                update(Trigger)
                .where(Trigger.id == self.trigger_id, Trigger.last_fired_at == self.fired_at)
                .values(
                    last_status=TriggerStatus.failed if error else TriggerStatus.succeeded,
                    last_error=error,
                )
            )
            store.commit()


async def fire(data_store_id: uuid.UUID, trigger_id: int, fired_at: datetime) -> None:
    """Fire one claimed trigger, and record how it went."""
    await firing.fire(ScheduleFiring(data_store_id, trigger_id, fired_at))


async def tick(now: datetime | None = None) -> dict[Claim, asyncio.Task]:
    """Claim what's due, as much as there are free slots for, and fire each in a task of its own;
    returns the tasks by (store, trigger id)."""
    now = now or datetime.now(UTC)
    # Even with no free slot: with one replica, nothing else would fail what a dead process left.
    await run_in_threadpool(_sweep)
    # Every fire counts, a workflow's too: it holds its slot only while it starts and queues
    # the run, and counting it keeps the bound simple and honest.
    free = min(BATCH, firing.free_slots())
    fired = {}
    if free <= 0:
        return fired
    for store_id, trigger_id in await run_in_threadpool(_claim, now, free):
        fired[store_id, trigger_id] = firing.launch(
            ScheduleFiring(store_id, trigger_id, now), name=f"trigger-{store_id}-{trigger_id}"
        )
    # Then the chat mentions' agent turns still waiting for a slot, with what's left.
    await chats_api.launch_turns()
    return fired


async def run_forever(interval: float) -> None:
    """Tick every `interval` seconds, and beat the fires in flight every `HEARTBEAT_SECONDS`,
    until cancelled; then stop the fires still running."""
    beating = asyncio.create_task(firing.beat_forever(HEARTBEAT_SECONDS), name="heartbeat")
    try:
        while True:
            try:
                await tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(interval)
    finally:
        beating.cancel()
        await firing.stop_all()
        await asyncio.gather(beating, return_exceptions=True)
