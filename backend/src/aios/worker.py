import asyncio
import uuid

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from aios.config import get_settings
from aios.datastores import DataStoreNotFound, store_session
from aios.datastores.resolve import data_store_of_document

settings = get_settings()

# Fire-and-forget tasks: no result backend; progress lives on the documents and workflow_runs
# tables.
celery_app = Celery("aios", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    # Soft limit raises inside the task (so ingest marks the row failed); the hard limit kills
    # a stuck child shortly after. These are ingest's; workflow runs set their own. Redis
    # redelivers unacked messages after visibility_timeout, so it must exceed the longest a
    # message can be held: up to one hard limit waiting behind the running task (prefetch) plus
    # its own run. Otherwise a slow doc is ingested twice at once.
    task_soft_time_limit=settings.ingest_time_limit,
    task_time_limit=settings.ingest_time_limit + 60,
    broker_transport_options={"visibility_timeout": 2 * (settings.ingest_time_limit + 60) + 600},
    # Runs take hours, so they get their own queue and worker (`-Q workflows`): an ingest never
    # waits behind one. The timeout above is broker-wide and shorter than a run, which is fine:
    # a redelivered run doesn't start again, and is only failed if its heartbeat has stopped
    # (see `aios.workflows._claim`).
    task_routes={"aios.run_workflow": {"queue": "workflows"}},
)


@celery_app.task(name="aios.ingest_document")
def ingest_document_task(data_store_id: str | int, document_id: int | None = None) -> None:
    """Ingest a document of the data store's (its id as a string: tasks' arguments are JSON)."""
    from aios.ingest import ingest_document

    if document_id is None:
        # Queued by the release before data stores, with only the document's id. Only needed
        # until that release's messages have drained (see `data_store_of_document`).
        document_id = int(data_store_id)
        store_id = data_store_of_document(document_id)
        if store_id is None:
            return
        data_store_id = str(store_id)
    try:
        with store_session(uuid.UUID(data_store_id), "rw") as db:
            ingest_document(db, document_id)
    except DataStoreNotFound:
        # Deleted since the upload (with its owner), and its documents with it: as for a
        # document deleted before its ingest, there's nothing left to do.
        return


@celery_app.task(
    name="aios.run_workflow",
    # A run times itself (`execute_run`); these are backstops, should its event loop be stuck.
    # The run itself is failed long before (`workflow_run_no_progress_limit`); these free the
    # worker process if its loop never comes back.
    soft_time_limit=settings.workflow_run_time_limit + 60,
    time_limit=settings.workflow_run_time_limit + 120,
)
def run_workflow_task(run_id: int) -> None:
    from aios import tracing
    from aios.workflows import execute_run, fail_run, time_limit_error

    try:
        # Only a queued run starts, so a redelivered message doesn't run it twice; one left
        # running by a worker that died (its heartbeat stopped) is failed instead.
        asyncio.run(execute_run(run_id))
    except SoftTimeLimitExceeded:
        fail_run(run_id, time_limit_error())
    finally:
        # A worker has no app shutdown to flush traces on (see `aios.main`).
        tracing.flush()
