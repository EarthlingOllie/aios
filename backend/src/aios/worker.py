import asyncio

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from aios.config import get_settings
from aios.db import SessionLocal

settings = get_settings()

# The longest any task can hold its message: the larger hard limit.
_longest_task = max(settings.ingest_time_limit, settings.workflow_run_time_limit) + 60

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
    broker_transport_options={"visibility_timeout": 2 * _longest_task + 600},
)


@celery_app.task(name="aios.ingest_document")
def ingest_document_task(document_id: int) -> None:
    from aios.ingest import ingest_document

    with SessionLocal() as db:
        ingest_document(db, document_id)


@celery_app.task(
    name="aios.run_workflow",
    soft_time_limit=settings.workflow_run_time_limit,
    time_limit=settings.workflow_run_time_limit + 60,
)
def run_workflow_task(run_id: int) -> None:
    from aios import tracing
    from aios.workflows import execute_run, fail_run

    try:
        # Only a queued run starts, so a redelivered message doesn't run it twice.
        asyncio.run(execute_run(run_id))
    except SoftTimeLimitExceeded:
        minutes = settings.workflow_run_time_limit // 60
        fail_run(run_id, f"The run took longer than {minutes} minutes, so it was stopped.")
    finally:
        # A worker has no app shutdown to flush traces on (see `aios.main`).
        tracing.flush()
