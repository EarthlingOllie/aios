import asyncio
import uuid

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from aios.config import get_settings
from aios.datastores import DataStoreBusy, DataStoreNotFound, DataStoreNotReady, store_session

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
        "aios.migrate_data_stores": {"queue": "provisioning"},
        "aios.retry_unfinished_data_stores": {"queue": "provisioning"},
        # Its `history` tables are migrations, so it's the provisioner's too. The export itself
        # writes as the store's `rw` role, as ingest does, so it's the app workers' (the
        # default queue).
        "aios.set_up_history_export": {"queue": "provisioning"},
    },
)

# Stores whose provisioning gave up are tried again this often (seconds), by the provisioning
# worker's beat (`celery worker -B`): there's one of it, and a second would only do it twice.
RETRY_UNFINISHED_EVERY = 600
# Stores with runs still waiting to be exported (their export was never queued, or was lost)
# are queued this often (seconds), by the same beat; the exports go to the app workers.
EXPORT_PENDING_HISTORY_EVERY = 60
celery_app.conf.beat_schedule = {
    "retry-unfinished-data-stores": {
        "task": "aios.retry_unfinished_data_stores",
        "schedule": RETRY_UNFINISHED_EVERY,
        # One that waited longer than this is stale: the next is due.
        "options": {"expires": RETRY_UNFINISHED_EVERY},
    },
    "export-pending-history": {
        "task": "aios.export_pending_history",
        "schedule": EXPORT_PENDING_HISTORY_EVERY,
        "options": {"expires": EXPORT_PENDING_HISTORY_EVERY},
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


# Provisioning gives up after this many tries (a minute apart, then longer), and marks the store
# failed; `retry_unfinished_data_stores_task` tries again, every `RETRY_UNFINISHED_EVERY`.
PROVISION_RETRIES = 5


@celery_app.task(name="aios.provision_data_store", bind=True, max_retries=PROVISION_RETRIES)
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


@celery_app.task(name="aios.retry_unfinished_data_stores")
def retry_unfinished_data_stores_task() -> None:
    """Try once more to provision stores that never got there, and bring ready stores behind
    the store tree's head to it (`aios.datastores.provision`)."""
    from aios.datastores import provision

    provision.retry_unfinished()


@celery_app.task(name="aios.migrate_data_stores")
def migrate_data_stores_task() -> None:
    """Bring every ready store to the store tree's head (`aios.datastores.provision`)."""
    from aios.datastores import provision

    provision.migrate_all()


@celery_app.task(name="aios.set_up_history_export")
def set_up_history_export_task(data_store_id: str) -> None:
    """Give the store its `history` tables, now its owner has switched the run history export
    on, then queue the export of what's waiting (`aios.datastores.history`). Safe to run again.

    A store that isn't ready yet gets them as it's provisioned, and its runs are exported by
    the periodic sweep (`export_pending_history_task`) once it is; one whose migration fails is
    tried again by `retry_unfinished_data_stores_task`."""
    from aios.datastores import history, provision

    store_id = uuid.UUID(data_store_id)
    try:
        provision.migrate_store(store_id)
    except LookupError:
        return
    history.enqueue_export(store_id)


@celery_app.task(name="aios.export_history", bind=True, max_retries=None)
def export_history_task(self, data_store_id: str) -> None:
    """Drain the store's run history outbox into its `history` tables
    (`aios.datastores.history`). Safe to run again, or at once with another for the store."""
    from aios.datastores import history

    try:
        history.export(uuid.UUID(data_store_id))
    except (DataStoreNotFound, DataStoreNotReady):
        # Deleted, or being deleted, since: its outbox goes with it.
        return
    except DataStoreBusy as exc:
        # Its rows are still pending; this delivery's retry picks them up.
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc


@celery_app.task(name="aios.export_pending_history")
def export_pending_history_task() -> None:
    """Queue the export of every exporting store with runs still waiting."""
    from aios.datastores import history

    for store_id in history.stores_to_export():
        history.enqueue_export(store_id)


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
