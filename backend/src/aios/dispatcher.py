"""The event dispatcher: fires event triggers on what the stores' outboxes record
(`aios.outbox`), on the run service.

- **Which stores.** A store whose outbox has events waits with a pending stamp on its registry
  row. Each run service process drains stamped stores when woken (`outbox.wake`) and every
  `event_dispatch_poll_seconds` regardless (`run_forever`). Claiming a store clears its stamp
  first (SKIP LOCKED, so each is claimed by one process), so events committed after that stamp
  it again. `drain_once` is the same pass, run once, for tests and tools.
- **Draining a store.** In order, it claims undispatched events (`event_dispatch_batch` at a
  time, skipping those another process holds), matches each against the store's enabled event
  triggers of its type and their filters (`outbox.matches`), applies the loop rules to each
  match, fires those that pass, and marks the events dispatched: all in one transaction, so an
  event is dispatched exactly when its firings and refusals are recorded.
- **The loop rules**, in order, stop automation feeding itself:
  1. **Own events:** not on an event its immediate cause (the invocation whose run caused it)
     is a run of the trigger's own target, unless the trigger has `fire_on_own_events`.
  2. **No repeats:** not if the trigger already started an invocation in the event's chain.
  3. **Depth:** not if the run it would start would make the chain longer than
     `chain_max_depth`.
  4. **Rate:** not past `event_trigger_rate_per_minute` firings in the last minute; the trigger
     shows "Rate limited".

  Each refusal is the trigger's last outcome (`refused`, or `rate_limited`, with why) and is
  audited (`trigger.refused`, by the system). No run starts.
- **Firing** starts a run as a schedule does (`aios.scheduler`), started by no member, with an
  invocation caused by the event (cause event, naming it, and continuing its chain):
  - an agent's: a new run of its current version, whose one turn is enqueued on the run
    service's background lane once the store has committed, told the trigger's `input` (if
    any) and the event, wrapped as untrusted (`outbox.agent_message`). The turn records how
    it went on the trigger, as a schedule's does (`aios.run_service.work`);
  - a workflow's: a run of its current version, whose input is the trigger's `input` as a
    template rendered with the event (`{{payload.name}}`, ...), or else the event as JSON
    (`workflow_input`), sent to the worker once the store has committed. The trigger then shows
    how the run goes. Workflows have no input schema yet to check it against.

  One that can't start (its target archived, say) is recorded as failed, without a run.
- **The sweep** (`sweep`, on the worker's beat every `event_sweep_interval_seconds`) stamps any
  store with undispatched events whose stamp was lost, and prunes events dispatched more than
  `event_retention_days` ago.
"""

import asyncio
import contextlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
import redis.asyncio as aioredis
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.orm import Session

from aios import audit, outbox
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.config import get_settings
from aios.datastores import ready_stores, store_id_of, store_session
from aios.datastores.models import Agent, Event, Invocation, Trigger, Workflow, WorkflowRun
from aios.db import SessionLocal
from aios.models import (
    DataStore,
    DataStoreStatus,
    InvocationCause,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
)
from aios.run_service import queue
from aios.run_service.work import Schedule, turn_work

log = logging.getLogger(__name__)

RATE_LIMITED = "Rate limited"


# What firing a trigger left to do once the store commits.
@dataclass
class _After:
    works: list[queue.Work] = field(default_factory=list)
    # Workflow runs to send to the worker: (trigger id, fired at, run id).
    workflow_runs: list[tuple[int, datetime, int]] = field(default_factory=list)


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def _lookup(document: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(document, dict) and part in document:
            document = document[part]
        elif isinstance(document, list) and part.isdigit() and int(part) < len(document):
            document = document[int(part)]
        else:
            return ""
    return document


def workflow_input(trigger: Trigger, event: Event) -> str:
    """An event trigger's workflow run's input: the trigger's `input` as a template, each
    `{{path}}` replaced by that field of the event (`outbox.event_document`: `{{payload.name}}`;
    objects as JSON, missing fields empty), or else the event as JSON."""
    document = outbox.event_document(event)
    if not trigger.input.strip():
        return json.dumps(document)

    def value(found: re.Match) -> str:
        v = _lookup(document, found.group(1))
        return v if isinstance(v, str) else json.dumps(v)

    return _PLACEHOLDER.sub(value, trigger.input)


@dataclass(frozen=True)
class _Refusal:
    status: TriggerStatus
    rule: str
    reason: str


def _refusal(store: Session, trigger: Trigger, event: Event) -> _Refusal | None:
    """The first loop rule that refuses firing the trigger on the event, if any."""
    settings = get_settings()
    if not trigger.fire_on_own_events and event.invocation_id is not None:
        cause = store.get(Invocation, event.invocation_id)
        if (
            cause is not None
            and cause.target_kind == trigger.target_kind
            and (cause.agent_id or cause.workflow_id) == (trigger.agent_id or trigger.workflow_id)
        ):
            return _Refusal(
                TriggerStatus.refused,
                "own_event",
                "Refused: its own target caused the event.",
            )
    if event.chain and store.scalar(
        select(exists().where(Invocation.id.in_(event.chain), Invocation.trigger_id == trigger.id))
    ):
        return _Refusal(
            TriggerStatus.refused,
            "repeat",
            "Refused: it already fired in this chain of causes, so it would loop.",
        )
    if len(event.chain) + 1 > settings.chain_max_depth:
        return _Refusal(
            TriggerStatus.refused,
            "depth",
            f"Refused: the chain of causes would be longer than {settings.chain_max_depth}.",
        )
    fired = store.scalar(
        select(func.count())
        .select_from(Invocation)
        .where(
            Invocation.trigger_id == trigger.id,
            Invocation.cause == InvocationCause.event,
            Invocation.created_at > func.now() - timedelta(minutes=1),
        )
    )
    if fired >= settings.event_trigger_rate_per_minute:
        return _Refusal(
            TriggerStatus.rate_limited,
            "rate",
            f"{RATE_LIMITED}: at most {settings.event_trigger_rate_per_minute} runs a minute.",
        )
    return None


def _refuse(
    store: Session, trigger: Trigger, event: Event, refusal: _Refusal, at: datetime
) -> None:
    """Record the refusal as the trigger's last outcome, which a run it started earlier, still
    going, no longer overwrites as it ends (it records on the firing that started it), and in
    the audit log."""
    trigger.last_fired_at = at
    # Not its last run's outcome any more (`api.triggers.last_outcome`).
    trigger.last_run_id = trigger.last_workflow_run_id = None
    trigger.last_status = refusal.status
    trigger.last_error = refusal.reason
    audit.record(
        store,
        audit.SYSTEM,
        "trigger.refused",
        "trigger",
        trigger.id,
        {"rule": refusal.rule, "reason": refusal.reason, "event_id": event.id},
    )


def _fire(
    store: Session,
    organisation_id: int,
    trigger: Trigger,
    event: Event,
    fired_at: datetime,
    after: _After,
) -> None:
    """Start the trigger's run for the event, in a savepoint: one that can't start is recorded
    as failed, without a run."""
    data_store_id = store_id_of(store)
    try:
        with store.begin_nested():
            if trigger.target_kind == TriggerTargetKind.agent:
                agent = store.get(Agent, trigger.agent_id)
                run = agents_api.new_agent_run(
                    store,
                    organisation_id,
                    agent,
                    created_by=None,
                    trigger_id=trigger.id,
                    event=event,
                )
                invocation_id = store.scalar(
                    select(Invocation.id).where(Invocation.agent_run_id == run.id)
                )
                trigger.last_run_id, trigger.last_workflow_run_id = run.id, None
                message = outbox.agent_message(trigger, event)
                after.works.append(
                    turn_work(
                        organisation_id,
                        data_store_id,
                        invocation_id,
                        Schedule(trigger.id, fired_at, message),
                    )
                )
            else:
                workflow = store.get(Workflow, trigger.workflow_id)
                wrun = workflows_api.new_workflow_run(
                    store,
                    organisation_id,
                    workflow,
                    workflow_input(trigger, event),
                    created_by=None,
                    trigger_id=trigger.id,
                    event=event,
                )
                trigger.last_run_id, trigger.last_workflow_run_id = None, wrun.id
                after.workflow_runs.append((trigger.id, fired_at, wrun.id))
            trigger.last_fired_at = fired_at
            trigger.last_status = TriggerStatus.running
            trigger.last_error = None
    except HTTPException as exc:
        _failed(trigger, fired_at, str(exc.detail))
    except Exception:
        log.exception("event trigger %s couldn't fire on event %s", trigger.id, event.id)
        _failed(trigger, fired_at, "Couldn't start the run.")


def _failed(trigger: Trigger, fired_at: datetime, error: str) -> None:
    trigger.last_fired_at = fired_at
    trigger.last_run_id = trigger.last_workflow_run_id = None
    trigger.last_status = TriggerStatus.failed
    trigger.last_error = error


def _organisation(data_store_id: uuid.UUID) -> int | None:
    with SessionLocal() as db:
        return db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )


def _drain_batch(data_store_id: uuid.UUID, organisation_id: int) -> tuple[int, _After]:
    """Dispatch the store's next batch of undispatched events; how many there were, and what to
    do once committed."""
    after = _After()
    with store_session(data_store_id) as store:
        events = store.scalars(
            select(Event)
            .where(Event.dispatched_at.is_(None))
            .order_by(Event.id)
            .limit(get_settings().event_dispatch_batch)
            .with_for_update(skip_locked=True)
        ).all()
        if not events:
            return 0, after
        types = {e.type for e in events}
        triggers = store.scalars(
            select(Trigger)
            .where(
                Trigger.kind == TriggerKind.event,
                Trigger.enabled.is_(True),
                Trigger.event_type.in_(types),
            )
            .order_by(Trigger.id)
        ).all()
        for event in events:
            for trigger in triggers:
                if trigger.event_type != event.type or not outbox.matches(
                    event.type, trigger.filter or {}, event.payload
                ):
                    continue
                refusal = _refusal(store, trigger, event)
                if refusal is not None:
                    _refuse(store, trigger, event, refusal, datetime.now(UTC))
                    continue
                _fire(store, organisation_id, trigger, event, datetime.now(UTC), after)
            event.dispatched_at = func.now()
        store.commit()
        for trigger_id, fired_at, run_id in after.workflow_runs:
            _send_workflow_run(store, trigger_id, fired_at, run_id)
    return len(events), after


def _send_workflow_run(store: Session, trigger_id: int, fired_at: datetime, run_id: int) -> None:
    """Send a committed workflow run to the worker; if it can't be, it's deleted (as
    `workflows_api.queue_workflow_run` does) and the firing recorded as failed."""
    run = store.get(WorkflowRun, run_id)
    if run is None:
        return
    try:
        workflows_api.queue_workflow_run(store, run)
    except HTTPException as exc:
        store.execute(
            update(Trigger)
            .where(Trigger.id == trigger_id, Trigger.last_fired_at == fired_at)
            .values(last_status=TriggerStatus.failed, last_error=str(exc.detail))
        )
        store.commit()


def _enqueue(works: list[queue.Work]) -> None:
    if not works:
        return
    with SessionLocal() as db:
        queue.enqueue(db, works)
        db.commit()
    queue.wake(*{w.lane for w in works})


def drain_store(data_store_id: uuid.UUID) -> int:
    """Dispatch every undispatched event in the store, a batch at a time; how many there
    were."""
    organisation_id = _organisation(data_store_id)
    if organisation_id is None:
        return 0
    total = 0
    while True:
        count, after = _drain_batch(data_store_id, organisation_id)
        _enqueue(after.works)
        total += count
        if count == 0:
            return total


def claim_pending(limit: int = 50) -> list[uuid.UUID]:
    """Claim up to `limit` stores stamped as having events pending, clearing their stamps."""
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                text(
                    "UPDATE data_stores SET events_pending_at = NULL WHERE id IN ("
                    " SELECT id FROM data_stores WHERE events_pending_at IS NOT NULL"
                    " AND status = :ready ORDER BY events_pending_at LIMIT :limit"
                    " FOR UPDATE SKIP LOCKED) RETURNING id"
                ),
                {"ready": DataStoreStatus.ready.name, "limit": limit},
            )
        )
        db.commit()
    return ids


def drain_once(data_store_id: uuid.UUID | None = None) -> int:
    """Dispatch the events of every store stamped pending (or just this store's, stamped or
    not), until none is left; how many events were dispatched. Tests' entry point."""
    total = 0
    if data_store_id is not None:
        return drain_store(data_store_id)
    while stores := claim_pending():
        for store_id in stores:
            try:
                total += drain_store(store_id)
            except Exception:
                log.exception("couldn't dispatch the events of data store %s", store_id)
                # Stamped again, for the next pass.
                outbox.pending(store_id)
                return total
    return total


async def _listen(woken: asyncio.Event) -> None:
    while True:
        client = aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        try:
            async with client.pubsub(ignore_subscribe_messages=True) as pubsub:
                await pubsub.subscribe(outbox.WAKE_CHANNEL)
                async for _ in pubsub.listen():
                    woken.set()
        except (redis.RedisError, OSError) as exc:
            log.warning("event dispatcher wake channel lost: %s", exc)
        finally:
            with contextlib.suppress(redis.RedisError, OSError):
                await client.aclose()
        await asyncio.sleep(get_settings().event_dispatch_poll_seconds)


async def run_forever() -> None:
    """Drain stamped stores whenever woken, and every `event_dispatch_poll_seconds`."""
    woken = asyncio.Event()
    listening = asyncio.create_task(_listen(woken))
    try:
        while True:
            woken.clear()
            try:
                await run_in_threadpool(drain_once)
            except Exception:
                log.exception("event dispatch failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    woken.wait(), timeout=get_settings().event_dispatch_poll_seconds
                )
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


@dataclass
class Swept:
    # Stores stamped again, their stamp lost; events pruned.
    stamped: list[uuid.UUID] = field(default_factory=list)
    pruned: int = 0


def sweep(now: datetime | None = None) -> Swept:
    """In every ready store: stamp it pending if it has undispatched events (its stamp was
    lost), and prune events dispatched more than `event_retention_days` before `now`."""
    settings = get_settings()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.event_retention_days)
    swept = Swept()
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id) as store:
                waiting = store.scalar(select(exists().where(Event.dispatched_at.is_(None))))
                pruned = store.execute(
                    delete(Event).where(
                        Event.dispatched_at.is_not(None), Event.dispatched_at < cutoff
                    )
                ).rowcount
                store.commit()
        except Exception:
            log.exception("couldn't sweep data store %s's events", store_id)
            continue
        swept.pruned += pruned
        if waiting:
            with SessionLocal() as db:
                stamped = db.execute(
                    update(DataStore)
                    .where(DataStore.id == store_id, DataStore.events_pending_at.is_(None))
                    .values(events_pending_at=func.now())
                ).rowcount
                db.commit()
            if stamped:
                log.warning("data store %s had events pending without a stamp", store_id)
                swept.stamped.append(store_id)
    if swept.stamped:
        outbox.wake()
    return swept
