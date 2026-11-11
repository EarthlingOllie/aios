"""The requeue sweep: enqueueing is after the store commits, so an enqueue can be lost (the
process stopped, or the platform database failed, in between). Every so often each store's
invocations left unstarted for over `queue_requeue_after_seconds` without an item in the queue
are enqueued again (`aios.run_service.work.lost`); enqueueing is idempotent, so one found
twice is queued once."""

import logging

from fastapi.concurrency import run_in_threadpool

from aios.config import get_settings
from aios.datastores import ready_stores, store_session
from aios.db import SessionLocal
from aios.run_service import queue, work

log = logging.getLogger(__name__)


def requeue() -> int:
    """Enqueue every store's lost invocations again; how many were. A store that can't be
    reached is left for the next sweep."""
    after = get_settings().queue_requeue_after_seconds
    requeued: list[queue.Work] = []
    for store_id, _ in ready_stores():
        try:
            with store_session(store_id, "ro") as store:
                lost = work.lost(store, after)
        except Exception:
            log.exception("couldn't sweep data store %s for lost work", store_id)
            continue
        if not lost:
            continue
        keys = [
            key
            for invocation_id, _ in lost
            for key in (
                work.start_key(store_id, invocation_id),
                work.turn_key(store_id, invocation_id),
            )
        ]
        with SessionLocal() as db:
            queued = queue.queued_keys(db, keys)
            missing = [
                w
                for invocation_id, w in lost
                if work.start_key(store_id, invocation_id) not in queued
                and work.turn_key(store_id, invocation_id) not in queued
            ]
            queue.enqueue(db, missing)
            db.commit()
        for w in missing:
            log.warning("requeued lost %s %s of data store %s", w.kind, w.payload, store_id)
        requeued.extend(missing)
    if requeued:
        queue.wake(*{w.lane for w in requeued})
    return len(requeued)


async def run() -> None:
    await run_in_threadpool(requeue)
