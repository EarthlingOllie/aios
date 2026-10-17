"""Each data store's storage cap (`aios.datastores.quota`): what it uses, measured on a schedule,
against two configured caps, and the writes refused past either."""

import psycopg
from conftest import add_document
from sqlalchemy import select
from test_provisioning import admin_connect

from aios import worker
from aios.config import Settings, get_settings, libpq_conninfo
from aios.datastores import data_store_for, provision
from aios.datastores.connections import dispose_engines
from aios.models import DataStore


def store_row(db, store_id) -> DataStore:
    db.expire_all()
    return db.scalar(select(DataStore).where(DataStore.id == store_id))


def test_both_caps_are_settings_with_defaults(monkeypatch):
    monkeypatch.delenv("DATA_STORE_DB_QUOTA_BYTES", raising=False)
    monkeypatch.delenv("DATA_STORE_FILES_QUOTA_BYTES", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.data_store_db_quota_bytes == 1024**3
    assert defaults.data_store_files_quota_bytes == 5 * 1024**3

    monkeypatch.setenv("DATA_STORE_DB_QUOTA_BYTES", "1000")
    monkeypatch.setenv("DATA_STORE_FILES_QUOTA_BYTES", "2000")
    configured = Settings(_env_file=None)
    assert (configured.data_store_db_quota_bytes, configured.data_store_files_quota_bytes) == (
        1000,
        2000,
    )


def test_the_schedule_records_each_stores_database_size_and_file_bytes(db, user, files_bucket):
    store_id = data_store_for(user.id)
    add_document(store_id, b"x" * 1000)
    add_document(store_id, b"y" * 234)
    assert store_row(db, store_id).usage_measured_at is None

    worker.measure_data_store_usage_task.apply().get()

    row = store_row(db, store_id)
    with admin_connect() as admin:
        size = admin.execute("SELECT pg_database_size(%s)", (row.dbname,)).fetchone()[0]
    assert row.files_bytes == 1234
    # Measured a moment ago: the same, give or take what Postgres wrote since.
    assert abs(row.db_bytes - size) < 1024 * 1024
    assert row.usage_measured_at is not None


def test_the_schedule_measures_every_ready_store_and_skips_one_that_fails(
    db, user, new_user, files_bucket, monkeypatch
):
    """One store that can't be measured (unreachable, say) doesn't stop the rest."""
    broken = data_store_for(new_user().id)
    provision.provision(broken)
    store_id = data_store_for(user.id)
    with psycopg.connect(
        libpq_conninfo(get_settings().data_store_provisioner_url), autocommit=True
    ) as conn:
        conn.execute(f'ALTER ROLE "{store_row(db, broken).role_prefix}_ro" CONNECTION LIMIT 0')
    dispose_engines()

    worker.measure_data_store_usage_task.apply().get()

    assert store_row(db, store_id).usage_measured_at is not None
    assert store_row(db, broken).usage_measured_at is None


def test_the_schedule_runs_every_few_minutes_on_the_provisioning_worker():
    entry = worker.celery_app.conf.beat_schedule["measure-data-store-usage"]
    assert entry["task"] == "aios.measure_data_store_usage"
    assert entry["schedule"] == worker.MEASURE_USAGE_EVERY <= 15 * 60
    route = worker.celery_app.conf.task_routes["aios.measure_data_store_usage"]
    assert route == {"queue": "provisioning"}
