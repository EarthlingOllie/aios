"""The run service's work queue (`aios.models.WorkItem`), in the platform database.

- **Enqueueing** (`enqueue`) is idempotent by an item's deduplication key, and happens after
  the store commits what the item is for; then the lane's processes are woken (`wake`).
- **Claiming** (`claim`) is one transaction in two stages:
  1. *Choose candidates.* Count each organisation's items in flight (leased, unexpired), rank
     its ready items (chat first, then by readiness), and keep only as many as its cap leaves
     room for (`Organisation.in_flight_cap`, else `queue_in_flight_cap`). Order the candidates
     interactive lane first, then by fewest in flight, then oldest.
  2. *Lease them.* Lock the candidates still free, skipping locked rows, and lease them: a new
     token, `queue_lease_seconds` from now, one more attempt.

  The cap is soft: two processes claiming at once can each fill an organisation's room, so it
  can overshoot by the few items each claims. That was accepted over serialising claims.
- **Leases** are extended while the item runs (`extend`). One that lapses (its process died)
  makes the item claimable again; one claimed more than `queue_max_attempts` times is given up
  on (`Claimed.exhausted`), and the work fails its invocation.
- **Done** items are deleted (`complete`), with whatever they enqueue next in the same
  transaction. An item handed back (`release`) is ready again at once or after a delay, counting
  the attempt or not.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import redis
from sqlalchemy import Integer, and_, case, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.models import Organisation, WorkItem, WorkKind, WorkLane

log = logging.getLogger(__name__)


def wake_channel(lane: WorkLane) -> str:
    return f"aios:run-service:wake:{lane.value}"


@dataclass(frozen=True)
class Work:
    """An item to enqueue: for the organisation's store, in a lane, of a kind, with its payload
    of store ids, and optionally a deduplication key (one item per key at a time)."""

    organisation_id: int
    data_store_id: uuid.UUID
    lane: WorkLane
    kind: WorkKind
    payload: dict[str, Any]
    dedup_key: str | None = None
    delay_seconds: float = 0


def enqueue(db: Session, works: list[Work]) -> None:
    """Add the items to the queue, in the caller's transaction; an item whose deduplication key
    is already queued is left out."""
    for work in works:
        ready_at = func.now()
        if work.delay_seconds:
            ready_at = func.now() + timedelta(seconds=work.delay_seconds)
        db.execute(
            insert(WorkItem)
            .values(
                organisation_id=work.organisation_id,
                data_store_id=work.data_store_id,
                lane=work.lane,
                kind=work.kind,
                payload=work.payload,
                dedup_key=work.dedup_key,
                ready_at=ready_at,
            )
            .on_conflict_do_nothing(index_elements=[WorkItem.dedup_key])
        )


def wake(*lanes: WorkLane) -> None:
    """Wake the run service processes waiting for work in these lanes (all, if none given), once
    what was enqueued is committed. Best effort: a process not woken finds it on its next
    poll."""
    try:
        client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        with client:
            for lane in lanes or tuple(WorkLane):
                client.publish(wake_channel(lane), "1")
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't wake the run service: %s", exc)


@dataclass(frozen=True)
class Claimed:
    """An item this process has leased."""

    id: int
    token: uuid.UUID
    organisation_id: int
    data_store_id: uuid.UUID
    lane: WorkLane
    kind: WorkKind
    payload: dict[str, Any]
    attempts: int

    @property
    def exhausted(self) -> bool:
        """Claimed more times than an item may be tried: its earlier attempts' processes died."""
        return self.attempts > get_settings().queue_max_attempts


def _free():
    return or_(WorkItem.leased_until.is_(None), WorkItem.leased_until <= func.now())


def claim(db: Session, limit: int, background_limit: int | None = None) -> list[Claimed]:
    """Claim up to `limit` ready items, at most `background_limit` of them background ones
    (None: no separate limit), fairly across organisations (see the module docstring). Commits;
    returns them in the order chosen."""
    settings = get_settings()
    if background_limit is None:
        background_limit = limit
    if limit <= 0:
        return []
    in_flight = (
        select(WorkItem.organisation_id, func.count().label("n"))
        .where(WorkItem.leased_until > func.now())
        .group_by(WorkItem.organisation_id)
        .subquery("in_flight")
    )
    n = func.coalesce(in_flight.c.n, 0)
    interactive_first = case((WorkItem.lane == WorkLane.interactive, 0), else_=1)
    ranked = (
        select(
            WorkItem.id,
            WorkItem.lane,
            WorkItem.ready_at,
            n.label("n"),
            interactive_first.label("lane_order"),
            func.coalesce(Organisation.in_flight_cap, literal(settings.queue_in_flight_cap))
            .cast(Integer)
            .label("cap"),
            func.row_number()
            .over(
                partition_by=WorkItem.organisation_id,
                order_by=(interactive_first, WorkItem.ready_at, WorkItem.id),
            )
            .label("rank"),
        )
        .join(Organisation, Organisation.id == WorkItem.organisation_id)
        .outerjoin(in_flight, in_flight.c.organisation_id == WorkItem.organisation_id)
        .where(WorkItem.ready_at <= func.now(), _free())
        .subquery("ranked")
    )
    candidates = db.execute(
        select(ranked.c.id, ranked.c.lane)
        .where(ranked.c.rank <= ranked.c.cap - ranked.c.n)
        .order_by(ranked.c.lane_order, ranked.c.n, ranked.c.ready_at, ranked.c.id)
        .limit(limit)
    ).all()
    chosen: list[int] = []
    background = 0
    for item_id, lane in candidates:
        if lane == WorkLane.background:
            if background >= background_limit:
                continue
            background += 1
        chosen.append(item_id)
    if not chosen:
        db.rollback()
        return []
    locked = db.scalars(
        select(WorkItem.id)
        .where(WorkItem.id.in_(chosen), WorkItem.ready_at <= func.now(), _free())
        .with_for_update(skip_locked=True)
    ).all()
    token = uuid.uuid4()
    rows = db.execute(
        update(WorkItem)
        .where(WorkItem.id.in_(locked))
        .values(
            lease_token=token,
            leased_until=func.now() + timedelta(seconds=settings.queue_lease_seconds),
            attempts=WorkItem.attempts + 1,
        )
        .returning(
            WorkItem.id,
            WorkItem.organisation_id,
            WorkItem.data_store_id,
            WorkItem.lane,
            WorkItem.kind,
            WorkItem.payload,
            WorkItem.attempts,
        )
    ).all()
    db.commit()
    order = {item_id: i for i, item_id in enumerate(chosen)}
    claimed = [
        Claimed(
            r.id, token, r.organisation_id, r.data_store_id, r.lane, r.kind, r.payload, r.attempts
        )
        for r in rows
    ]
    return sorted(claimed, key=lambda c: order[c.id])


def _held(item: Claimed):
    return and_(WorkItem.id == item.id, WorkItem.lease_token == item.token)


def extend(db: Session, items: list[Claimed]) -> set[int]:
    """Extend the leases of these items while they're still this process's; the ids of those
    that were. Commits."""
    if not items:
        return set()
    held = or_(*(_held(i) for i in items))
    ids = db.scalars(
        update(WorkItem)
        .where(held)
        .values(leased_until=func.now() + timedelta(seconds=get_settings().queue_lease_seconds))
        .returning(WorkItem.id)
    ).all()
    db.commit()
    return set(ids)


def complete(db: Session, item: Claimed, then: list[Work] | None = None) -> None:
    """The item is done: delete it (if still this process's), and enqueue what follows it, in
    one transaction. Commits."""
    db.execute(delete(WorkItem).where(_held(item)))
    enqueue(db, then or [])
    db.commit()


def release(
    db: Session, item: Claimed, *, delay_seconds: float = 0, count_attempt: bool = True
) -> None:
    """Hand the item back to the queue (if still this process's): ready again after
    `delay_seconds`; without `count_attempt`, its claim doesn't count as an attempt. Commits."""
    db.execute(
        update(WorkItem)
        .where(_held(item))
        .values(
            lease_token=None,
            leased_until=None,
            ready_at=func.now() + timedelta(seconds=delay_seconds),
            attempts=WorkItem.attempts if count_attempt else WorkItem.attempts - 1,
        )
    )
    db.commit()


def queued_keys(db: Session, keys: list[str]) -> set[str]:
    """Which of these deduplication keys have an item in the queue."""
    if not keys:
        return set()
    return set(db.scalars(select(WorkItem.dedup_key).where(WorkItem.dedup_key.in_(keys))))
