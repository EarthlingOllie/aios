import asyncio
import uuid

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from aios.config import get_settings
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_session,
)

settings = get_settings()

# Fire-and-forget tasks: no result backend; progress lives on the documents (in data stores) and
# workflow_runs tables.
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
    task_routes={
        "aios.run_workflow": {"queue": "workflows"},
        # Only the provisioning worker has the provisioner's credentials (`aios.datastores`).
        "aios.provision_data_store": {"queue": "provisioning"},
        "aios.retry_unfinished_data_stores": {"queue": "provisioning"},
    },
)

# Stores whose provisioning gave up are tried again this often (seconds), by the provisioning
# worker's beat (`celery worker -B`): there's one of it, and a second would only do it twice.
RETRY_UNFINISHED_EVERY = 600
celery_app.conf.beat_schedule = {
    "retry-unfinished-data-stores": {
        "task": "aios.retry_unfinished_data_stores",
        "schedule": RETRY_UNFINISHED_EVERY,
        # One that waited longer than this is stale: the next is due.
        "options": {"expires": RETRY_UNFINISHED_EVERY},
    },
}


# A busy store (its role at its connection limit) is tried again this often (seconds), for as long
# as it stays busy: giving up would leave the document pending with nothing left to ingest it, and
# marking it failed needs the connection the store hasn't got free.
BUSY_RETRY_COUNTDOWN = 30


@celery_app.task(name="aios.ingest_document", bind=True, max_retries=None)
def ingest_document_task(self, data_store_id: str, document_id: int) -> None:
    """Ingest a document in the data store (its id as a string: tasks' arguments are JSON)."""
    from aios.ingest import ingest_document

    try:
        with store_session(uuid.UUID(data_store_id), "rw") as db:
            ingest_document(db, document_id)
    except DataStoreBehind as exc:
        # Ready, but not yet at the store tree's head this code needs: the schedule brings it
        # there in minutes (`provision.retry_unfinished`).
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc
    except (DataStoreNotFound, DataStoreNotReady):
        # Deleted since the upload (with its owner), or being deleted: a store is ready by the
        # time anything is uploaded to it, and stops being ready only when it's deleted. Its
        # documents go with it, so, as for a document deleted before its ingest, there's
        # nothing left to do.
        return
    except DataStoreBusy as exc:
        # Its role's connections are all in use; the document is still pending (or, claimed,
        # processing), and the next delivery picks it up.
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc


# Provisioning is retried this many times after its first try (a minute apart, then longer:
# about half an hour in all), then marks the store failed; `retry_unfinished_data_stores_task`
# tries again, every `RETRY_UNFINISHED_EVERY`.
PROVISION_RETRIES = 5


# Provisioning a store takes seconds: these stop a stuck run (waiting on a lock, say) holding
# the provisioning worker for ingest's half hour. A run cut short is finished by running it again.
PROVISION_TIME_LIMIT = 5 * 60


@celery_app.task(
    name="aios.provision_data_store",
    bind=True,
    max_retries=PROVISION_RETRIES,
    soft_time_limit=PROVISION_TIME_LIMIT,
    time_limit=PROVISION_TIME_LIMIT + 60,
)
def provision_data_store_task(self, data_store_id: str) -> None:
    """Create the store's database (`aios.datastores.provision`). Safe to run again, or at once
    with another run for the same store."""
    from aios.datastores import provision

    store_id = uuid.UUID(data_store_id)
    try:
        provision.provision(store_id)
    except Exception as exc:
        if self.request.retries >= PROVISION_RETRIES:
            provision.fail(store_id, provision.describe_error(exc))
            raise
        raise self.retry(countdown=60 * 2**self.request.retries) from exc


@celery_app.task(
    name="aios.retry_unfinished_data_stores",
    # One that runs over (stores failing slowly, say) stops: the next, due by then, carries on.
    soft_time_limit=RETRY_UNFINISHED_EVERY,
    time_limit=RETRY_UNFINISHED_EVERY + 60,
)
def retry_unfinished_data_stores_task() -> None:
    """Try once more to provision stores that never got there, and bring ready stores behind
    the store tree's head to it (`aios.datastores.provision`)."""
    from aios.datastores import provision

    provision.retry_unfinished()


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
