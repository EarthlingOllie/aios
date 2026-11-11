"""The runs a chat's mentions start, on the run service (`aios.run_service`): each queued
invocation's start is enqueued after the store commits it, in the interactive lane, and the
run service runs its handoff and then its run (`aios.run_service.work`). An invocation is named
by its store and its id there."""

import logging
import uuid

from sqlalchemy.orm import Session

from aios.run_service import queue, work

log = logging.getLogger(__name__)


def enqueue_starts(
    db: Session, organisation_id: int, data_store_id: uuid.UUID, invocation_ids: list[int]
) -> None:
    """Enqueue the start of each of the store's (committed) invocations on the run service, and
    commit. One whose enqueue is lost (the platform database failing now) is put back by the
    requeue sweep (`aios.run_service.sweep`), so this never fails the post."""
    if not invocation_ids:
        return
    try:
        queue.enqueue(
            db, [work.start_work(organisation_id, data_store_id, i) for i in invocation_ids]
        )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("couldn't enqueue the starts of invocations %s", invocation_ids)
