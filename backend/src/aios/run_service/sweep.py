"""The run service's sweeps.

- **Requeue**: enqueueing is after the store commits, so an enqueue can be lost (the process
  stopped, or the platform database failed, in between). Every so often each store's
  invocations left unstarted for over `queue_requeue_after_seconds` without an item in the queue
  are enqueued again (`aios.run_service.work.lost`), and so are those handing back without a
  handback queued; enqueueing is idempotent, so one found twice is queued once.
- **Embedding**: every `search_embed_interval_seconds` each process enqueues an embedding sweep
  per ready store (`aios.run_service.work.embedding_work`), queued once however many ask.
- **Syncs**: every `sync_poll_seconds` each process enqueues a sync of every ready store's
  sources that are due (`aios.sync.due`), queued once per source however many ask, and prunes
  sync records past `sync_history_days` (`aios.sync.prune`). It also finishes removing connectors
  whose removal stopped part way (`aios.connectors.removals_due`)."""

import logging

from fastapi.concurrency import run_in_threadpool

from aios import connectors, sync
from aios.config import get_settings
from aios.datastores import ready_stores, store_session
from aios.db import SessionLocal
from aios.models import WorkKind, WorkLane
from aios.run_service import queue, work

log = logging.getLogger(__name__)


def _keys(store_id, invocation_id: int, w: queue.Work) -> tuple[str, ...]:
    """The keys of the items that carry the invocation on, any of which queued means it isn't
    lost."""
    if w.kind == WorkKind.handback:
        return (work.handback_key(store_id, invocation_id),)
    return (work.start_key(store_id, invocation_id), work.turn_key(store_id, invocation_id))


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

        with SessionLocal() as db:
            queued = queue.queued_keys(db, [k for i, w in lost for k in _keys(store_id, i, w)])
            missing = [w for i, w in lost if not queued.intersection(_keys(store_id, i, w))]
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


def _enqueue_embedding() -> int:
    works = [
        work.embedding_work(organisation_id, store_id)
        for store_id, organisation_id in ready_stores()
    ]
    with SessionLocal() as db:
        queue.enqueue(db, works)
        db.commit()
    if works:
        queue.wake(WorkLane.background)
    return len(works)


async def enqueue_embedding() -> None:
    """Enqueue an embedding sweep of every ready store (a no-op for one already queued)."""
    await run_in_threadpool(_enqueue_embedding)


def _enqueue_syncs() -> int:
    works: list[queue.Work] = []
    for store_id, organisation_id in ready_stores():
        try:
            with store_session(store_id, "rw") as store:
                due = sync.due(store)
                sync.prune(store)
                removing = connectors.removals_due(store)
                store.commit()
            for connector_id in removing:
                connectors.finish_removal(store_id, connector_id)
        except Exception:
            log.exception("couldn't look for data store %s's sources due", store_id)
            continue
        works.extend(work.sync_work(organisation_id, store_id, source_id) for source_id in due)
    if works:
        with SessionLocal() as db:
            queue.enqueue(db, works)
            db.commit()
        queue.wake(WorkLane.sources)
    return len(works)


async def enqueue_syncs() -> None:
    """Enqueue a sync of every ready store's sources that are due (a no-op for one queued)."""
    await run_in_threadpool(_enqueue_syncs)
