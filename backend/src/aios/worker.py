import uuid

from celery import Celery
from celery.schedules import crontab

from aios.config import get_settings
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    store_session,
)

settings = get_settings()

# Celery's queues, each with its own worker service (`docker-compose.yml`, `infra/terraform`):
# document ingest; urgent maintenance (applying approved changes, restores), which never waits
# behind routine maintenance (provisioning, migrations, measuring, purging, expiry, retention,
# sweeps). The routine worker runs the beat, which only enqueues.
INGEST = "ingest"
URGENT = "urgent"
ROUTINE = "routine"
QUEUES = (INGEST, URGENT, ROUTINE)
# Queues nothing is sent to any more, that the routine worker drains of what was left on them
# from before (`aios.queue_metrics` publishes their depth): the provisioning queue, split into
# urgent and routine. Once one stays empty, it's dropped from here and from the routine worker.
RETIRED_QUEUES = ("provisioning",)

# Fire-and-forget tasks: no result backend; progress lives on the documents (in data stores).
# Agents' turns and workflow runs are the run service's (`aios.run_service`), not Celery's.
celery_app = Celery("aios", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    # Soft limit raises inside the task (so ingest marks the row failed); the hard limit kills
    # a stuck child shortly after. These are ingest's. Redis
    # redelivers unacked messages after visibility_timeout, so it must exceed the longest a
    # message can be held: up to one hard limit waiting behind the running task (prefetch) plus
    # its own run. Otherwise a slow doc is ingested twice at once.
    task_soft_time_limit=settings.ingest_time_limit,
    task_time_limit=settings.ingest_time_limit + 60,
    broker_transport_options={"visibility_timeout": 2 * (settings.ingest_time_limit + 60) + 600},
    # Unrouted tasks are ingest's: documents, their checks and their embeddings.
    task_default_queue=INGEST,
    task_routes={
        # Urgent maintenance: what someone is waiting on, so never behind migrations or sweeps.
        # Its worker, and the routine one, are the only ones with the provisioner's credentials
        # (`aios.datastores`).
        "aios.apply_schema_change": {"queue": URGENT},
        "aios.restore_dropped": {"queue": URGENT},
        # Agents' logins and their grants (`aios.datastores.agent_roles`): a write waits on it.
        "aios.apply_agent_access": {"queue": URGENT},
        # Routine maintenance: provisioning and its retries, the stores' migrations, measuring,
        # purging, expiry, retention, the event sweep and reconciliation, and what the beat
        # fans out from here.
        "aios.provision_data_store": {"queue": ROUTINE},
        "aios.retry_unfinished_data_stores": {"queue": ROUTINE},
        "aios.delete_data_stores": {"queue": ROUTINE},
        "aios.delete_due_organisations": {"queue": ROUTINE},
        "aios.repair_stores": {"queue": ROUTINE},
        "aios.drain_audit_outbox": {"queue": ROUTINE},
        "aios.measure_data_store_usage": {"queue": ROUTINE},
        "aios.purge_dropped": {"queue": ROUTINE},
        "aios.expire_schema_change_requests": {"queue": ROUTINE},
        "aios.prune_retention": {"queue": ROUTINE},
        "aios.sweep_events": {"queue": ROUTINE},
        "aios.publish_queue_metrics": {"queue": ROUTINE},
        "aios.check_stored_content_sweep": {"queue": ROUTINE},
        "aios.check_jev_health": {"queue": ROUTINE},
        # Ingest: documents, and the per-store work the routine sweeps queue for it.
        "aios.ingest_document": {"queue": INGEST},
        "aios.check_document": {"queue": INGEST},
        "aios.check_stored_content": {"queue": INGEST},
    },
)

# Stores whose provisioning gave up are tried again this often (seconds), by the routine
# worker's beat (`celery worker -B`): there's one of it, and a second would only do it twice.
RETRY_UNFINISHED_EVERY = 600
# Stores being deleted (their organisations were) are swept this often (seconds), by the same
# beat, in case the sweep queued on deleting one was lost or failed.
DELETE_STORES_EVERY = RETRY_UNFINISHED_EVERY
# What each store uses is measured this often (seconds), for its storage cap
# (`aios.datastores.quota`): a store past it is refused writes from the next measurement.
MEASURE_USAGE_EVERY = 300
# Dropped tables and columns past their retention (days) are purged this often (seconds).
PURGE_DROPPED_EVERY = 3600
# The repair sweep (`repair_stores_task`) runs this often (seconds), by the same beat.
REPAIR_STORES_EVERY = 600
celery_app.conf.beat_schedule = {
    "retry-unfinished-data-stores": {
        "task": "aios.retry_unfinished_data_stores",
        "schedule": RETRY_UNFINISHED_EVERY,
        # One that waited longer than this is stale: the next is due.
        "options": {"expires": RETRY_UNFINISHED_EVERY},
    },
    "delete-data-stores": {
        "task": "aios.delete_data_stores",
        "schedule": DELETE_STORES_EVERY,
        "options": {"expires": DELETE_STORES_EVERY},
    },
    # Organisations whose deletion's grace period is over (`aios.organisations.delete_due`).
    "delete-due-organisations": {
        "task": "aios.delete_due_organisations",
        "schedule": settings.organisation_deletion_check_seconds,
        "options": {"expires": settings.organisation_deletion_check_seconds},
    },
    "measure-data-store-usage": {
        "task": "aios.measure_data_store_usage",
        "schedule": MEASURE_USAGE_EVERY,
        "options": {"expires": MEASURE_USAGE_EVERY},
    },
    "purge-dropped": {
        "task": "aios.purge_dropped",
        "schedule": PURGE_DROPPED_EVERY,
        "options": {"expires": PURGE_DROPPED_EVERY},
    },
    # Schema change requests nobody decided in time (`SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`).
    "expire-schema-change-requests": {
        "task": "aios.expire_schema_change_requests",
        "schedule": settings.schema_change_expiry_check_seconds,
        "options": {"expires": settings.schema_change_expiry_check_seconds},
    },
    "repair-stores": {
        "task": "aios.repair_stores",
        "schedule": REPAIR_STORES_EVERY,
        "options": {"expires": REPAIR_STORES_EVERY},
    },
    # Runs and chat history past each organisation's retention (`aios.retention`), and old read
    # notifications, nightly at a quiet hour. A day's run that waited past the next is stale.
    "prune-retention": {
        "task": "aios.prune_retention",
        "schedule": crontab(minute=0, hour=settings.retention_prune_hour),
        "options": {"expires": 24 * 60 * 60},
    },
    # Stored content awaiting Jev's check, or checked by an older version of it
    # (`aios.verdicts.sweep`), on the app workers, as ingest is.
    "check-stored-content": {
        "task": "aios.check_stored_content_sweep",
        "schedule": settings.jev_sweep_interval_seconds,
        "options": {"expires": settings.jev_sweep_interval_seconds},
    },
    "check-jev-health": {
        "task": "aios.check_jev_health",
        "schedule": settings.jev_health_interval_seconds,
        "options": {"expires": settings.jev_health_interval_seconds},
    },
    # Platform-side audit entries, to their stores (`aios.audit.drain`).
    "drain-audit-outbox": {
        "task": "aios.drain_audit_outbox",
        "schedule": settings.audit_drain_seconds,
        "options": {"expires": settings.audit_drain_seconds},
    },
    # Stores whose events pending stamp was lost, and old dispatched events
    # (`aios.dispatcher.sweep`).
    # Run service lanes' and Celery queues' depth and oldest ready age (`aios.queue_metrics`),
    # for autoscaling. One that waited past the next is stale.
    "publish-queue-metrics": {
        "task": "aios.publish_queue_metrics",
        "schedule": settings.queue_metrics_interval_seconds,
        "options": {"expires": settings.queue_metrics_interval_seconds},
    },
    "sweep-events": {
        "task": "aios.sweep_events",
        "schedule": settings.event_sweep_interval_seconds,
        "options": {"expires": settings.event_sweep_interval_seconds},
    },
}


# A busy store (its role at its connection limit) is tried again this often (seconds), for as long
# as it stays busy: giving up would leave the document pending with nothing left to ingest it, and
# marking it failed needs the connection the store hasn't got free.
BUSY_RETRY_COUNTDOWN = 30

# S3 that can't be reached (or is throttling, or failing on its side, past its client's own
# retries) is tried again this often (seconds), up to this many times: about ten minutes. Then
# the document fails with S3's error.
FILES_RETRY_COUNTDOWN = 60
FILES_RETRIES = 10


@celery_app.task(name="aios.ingest_document", bind=True, max_retries=None)
def ingest_document_task(self, data_store_id: str, document_id: int) -> None:
    """Ingest a document in the data store (its id as a string: tasks' arguments are JSON)."""
    from aios.datastores.files import FilesUnavailable
    from aios.ingest import ingest_document
    from aios.jev import JevUnavailable

    try:
        with store_session(uuid.UUID(data_store_id), "rw") as db:
            ingest_document(db, document_id, last_try=self.request.retries >= FILES_RETRIES)
    except FilesUnavailable as exc:
        raise self.retry(countdown=FILES_RETRY_COUNTDOWN) from exc
    except JevUnavailable:
        # Ingested, awaiting Jev's check: that's tried again on its own, not the ingest.
        check_document_task.apply_async(
            args=[data_store_id, document_id], countdown=JEV_RETRY_COUNTDOWN
        )
    except DataStoreBehind as exc:
        # Ready, but not yet at the store tree's head this code needs: the schedule brings it
        # there in minutes (`provision.retry_unfinished`).
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc
    except (DataStoreNotFound, DataStoreNotReady):
        # Deleted since the upload (with its organisation), or being deleted: a store is ready by
        # the time anything is uploaded to it, and stops being ready only when it's deleted. Its
        # documents go with it, so, as for a document deleted before its ingest, there's nothing
        # left to do.
        return
    except DataStoreBusy as exc:
        # Its role's connections are all in use; the document is still pending (or, claimed,
        # processing), and the next delivery picks it up.
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc


# A document Jev couldn't check at ingestion is checked again this often (seconds), this many
# times (about ten minutes); then it waits for the awaiting-check sweep.
JEV_RETRY_COUNTDOWN = 60
JEV_RETRIES = 10


@celery_app.task(name="aios.check_document", bind=True, max_retries=JEV_RETRIES)
def check_document_task(self, data_store_id: str, document_id: int) -> None:
    """Jev's check of an ingested document it couldn't check at ingestion
    (`aios.verdicts.check_document`), tried again while it can't."""
    from aios import verdicts
    from aios.jev import Verdict

    try:
        with store_session(uuid.UUID(data_store_id), "rw") as store:
            verdict = verdicts.check_document(store, document_id)
    except (DataStoreNotFound, DataStoreNotReady):
        return
    except (DataStoreBehind, DataStoreBusy) as exc:
        raise self.retry(countdown=BUSY_RETRY_COUNTDOWN) from exc
    if verdict == Verdict.awaiting_check and self.request.retries < JEV_RETRIES:
        raise self.retry(countdown=JEV_RETRY_COUNTDOWN)


@celery_app.task(name="aios.check_stored_content")
def check_stored_content_task(data_store_id: str) -> None:
    """The store's stored content checked again where it's due (`aios.verdicts.sweep`):
    awaiting a check, judged by an older version of Jev, or released and since changed."""
    from aios import verdicts

    try:
        verdicts.sweep(uuid.UUID(data_store_id))
    except (DataStoreNotFound, DataStoreNotReady, DataStoreBehind, DataStoreBusy):
        return  # gone, going, behind or busy: the next sweep tries it again


@celery_app.task(name="aios.check_stored_content_sweep")
def check_stored_content_sweep_task() -> None:
    """Queue `check_stored_content_task` for every ready store, each dropped if it hasn't
    started by the next sweep."""
    from aios.datastores import ready_stores

    every = settings.jev_sweep_interval_seconds
    for store_id, _ in ready_stores():
        check_stored_content_task.apply_async(args=[str(store_id)], expires=every)


@celery_app.task(name="aios.check_jev_health")
def check_jev_health_task() -> None:
    """Poll Jev's health (`aios.jev_health.poll`): an alert and metrics while it's down, and the
    notice people see."""
    from aios import jev_health

    jev_health.poll()


# Provisioning is retried this many times after its first try (a minute apart, then longer:
# about half an hour in all), then marks the store failed; `retry_unfinished_data_stores_task`
# tries again, every `RETRY_UNFINISHED_EVERY`.
PROVISION_RETRIES = 5


# Provisioning a store takes seconds: these stop a stuck run (waiting on a lock, say) holding
# the routine worker for ingest's half hour. A run cut short is finished by running it again.
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
    name="aios.delete_data_stores",
    # As the retry: one that runs over stops, and the next carries on where it left off.
    soft_time_limit=DELETE_STORES_EVERY,
    time_limit=DELETE_STORES_EVERY + 60,
)
def delete_data_stores_task(data_store_id: str | None = None) -> None:
    """Drop the databases, roles and files of the stores being deleted, and mark them deleted
    (`aios.datastores.provision`): the one given (just put into `deleting`), or every one."""
    from aios.datastores import provision

    if data_store_id is None:
        provision.delete_deleting()
    else:
        # Waiting for its provisioning, if that's still on it (it was deleted straight away).
        provision.delete_deleting([uuid.UUID(data_store_id)], wait=True)


@celery_app.task(name="aios.delete_due_organisations")
def delete_due_organisations_task() -> None:
    """Delete the organisations whose deletion's grace period is over, recording each, and queue
    their stores' teardown (`aios.organisations.delete_due`)."""
    from aios import organisations

    organisations.delete_due()


@celery_app.task(
    name="aios.measure_data_store_usage",
    # As the retry: one that runs over stops, and the next carries on.
    soft_time_limit=MEASURE_USAGE_EVERY,
    time_limit=MEASURE_USAGE_EVERY + 60,
)
def measure_data_store_usage_task() -> None:
    """Record what each ready store uses, for its storage cap (`aios.datastores.quota`)."""
    from aios.datastores import quota

    quota.measure_all()


@celery_app.task(
    name="aios.repair_stores",
    soft_time_limit=REPAIR_STORES_EVERY,
    time_limit=REPAIR_STORES_EVERY + 60,
)
def repair_stores_task() -> None:
    """The repair sweep: bring every ready store's people mirror back in line with its
    organisation's memberships (`aios.datastores.people.repair`), and the schedule due index
    back in line with every ready store's triggers (`aios.schedules.repair`), logging what it
    fixed."""
    from aios import schedules
    from aios.datastores import people

    people.repair()
    schedules.repair()


@celery_app.task(name="aios.drain_audit_outbox")
def drain_audit_outbox_task() -> None:
    """Deliver the platform's audit outbox to each organisation's store (`aios.audit.drain`)."""
    from aios import audit

    audit.drain()


# Applying a schema change takes seconds, but waits on locks (each up to its lock timeout).
APPLY_TIME_LIMIT = 5 * 60


# Applying a request (then enqueueing the resume of the run that made it) is tried this many
# times more if it fails (the database unreachable, say), further apart each time: both can be
# run again safely.
DECISION_RETRIES = 5


@celery_app.task(
    name="aios.apply_schema_change",
    soft_time_limit=APPLY_TIME_LIMIT,
    time_limit=APPLY_TIME_LIMIT + 60,
    autoretry_for=(Exception,),
    max_retries=DECISION_RETRIES,
    retry_backoff=True,
)
def apply_schema_change_task(data_store_id: str, request_id: str) -> None:
    """Apply an approved schema change request to its store, then resume the run that asked
    with how it went (`aios.datastores.schema_changes.apply`). Run again (redelivered), it
    applies nothing twice, and queues the resume if the run hasn't been given the outcome."""
    from aios.datastores import agent_roles, schema_changes

    store_id, request = uuid.UUID(data_store_id), uuid.UUID(request_id)
    schema_changes.apply(store_id, request)
    # A new table's owner may have no login yet: made here, before the run resumes to write.
    agent_roles.apply(store_id)
    enqueue_resume(store_id, request)


@celery_app.task(
    name="aios.restore_dropped",
    soft_time_limit=APPLY_TIME_LIMIT,
    time_limit=APPLY_TIME_LIMIT + 60,
    autoretry_for=(Exception,),
    max_retries=DECISION_RETRIES,
    retry_backoff=True,
)
def restore_dropped_task(data_store_id: str, dropped_id: int) -> None:
    """Restore a dropped table or column a member asked for (`aios.datastores.dropped`). Run
    again (redelivered), it finds nothing left to restore."""
    from aios.datastores import dropped

    dropped.restore(uuid.UUID(data_store_id), dropped_id)


@celery_app.task(
    name="aios.apply_agent_access",
    soft_time_limit=APPLY_TIME_LIMIT,
    time_limit=APPLY_TIME_LIMIT + 60,
    autoretry_for=(Exception,),
    max_retries=DECISION_RETRIES,
    retry_backoff=True,
)
def apply_agent_access_task(data_store_id: str) -> None:
    """Make the store's agents' logins and derive their grants from its ownership records
    (`aios.datastores.agent_roles`): after a share, transfer, archive and the like. Run again,
    it derives the same."""
    from aios.datastores import agent_roles

    agent_roles.apply(uuid.UUID(data_store_id))


@celery_app.task(
    name="aios.purge_dropped",
    soft_time_limit=PURGE_DROPPED_EVERY,
    time_limit=PURGE_DROPPED_EVERY + 60,
)
def purge_dropped_task() -> None:
    """Drop for good, in every ready store, the tables and columns dropped longer ago than
    `DATA_STORE_DROPPED_RETENTION_DAYS` (`aios.datastores.dropped`)."""
    from aios.datastores import dropped

    dropped.purge_all()


@celery_app.task(name="aios.sweep_events")
def sweep_events_task() -> None:
    """Stamp every store with undispatched events whose stamp was lost, so the dispatcher comes
    to it, and prune events dispatched past their retention (`aios.dispatcher.sweep`)."""
    from aios import dispatcher

    dispatcher.sweep()


@celery_app.task(name="aios.publish_queue_metrics")
def publish_queue_metrics_task() -> None:
    """Publish the run service lanes' and Celery queues' metrics (`aios.queue_metrics`)."""
    from aios import queue_metrics

    queue_metrics.publish()


@celery_app.task(name="aios.prune_retention")
def prune_retention_task() -> None:
    """Prune every store's runs and chat history past its organisation's retention, in batches,
    recording each prune in its audit log (`aios.retention.prune_all`); read notifications
    past `notification_retention_days` (`aios.notifications.prune`); and store usage samples
    past `storage_history_days` (`aios.datastores.quota.prune_samples`)."""
    from aios import notifications, retention
    from aios.datastores import quota

    try:
        retention.prune_all()
    finally:
        try:
            notifications.prune()
        finally:
            quota.prune_samples()


@celery_app.task(name="aios.expire_schema_change_requests")
def expire_schema_change_requests_task() -> None:
    """Expire the schema change requests nobody decided in time, and resume the runs that asked
    with that, as a rejection would (`aios.datastores.schema_changes.expire`)."""
    from aios.datastores import schema_changes

    schema_changes.expire()
    for store_id, request_id in schema_changes.expired_not_resumed():
        enqueue_resume(store_id, request_id)


def enqueue_resume(data_store_id: uuid.UUID, request_id: uuid.UUID) -> None:
    """Enqueue on the run service carrying on the run that made a request of the store, if it's
    decided and the run hasn't been given the outcome yet (`aios.run_service.work`): a workflow
    run's advance, which resumes its waiting node, or an agent run's resume turn."""
    from aios.run_service import work

    work.enqueue_resume(data_store_id, request_id)
