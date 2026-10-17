"""Each data store's storage cap (`aios.datastores.quota`): what it uses, measured on a schedule,
against two configured caps, and the writes refused past either."""

import psycopg
import pytest
from conftest import add_document
from sqlalchemy import select
from test_documents import PDF, stored_keys, upload
from test_provisioning import admin_connect

from aios import worker
from aios.api import documents
from aios.config import Settings, get_settings, libpq_conninfo
from aios.datastores import data_store_for, provision, quota
from aios.datastores.connections import dispose_engines
from aios.models import DataStore


@pytest.fixture
def queued(monkeypatch) -> list[tuple]:
    """Uploads' ingests, kept here instead of sent to the broker."""
    calls = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: calls.append(a))
    return calls


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


def test_a_store_at_its_file_cap_refuses_uploads_until_its_owner_deletes_some(
    api, user, queued, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_files_quota_bytes", len(PDF))
    store_id = data_store_for(user.id)
    first = upload(api).json()
    # Under the cap until it's measured at it.
    worker.measure_data_store_usage_task.apply().get()

    r = upload(api, name="more.pdf")

    assert r.status_code == 507
    assert r.json()["detail"] == (
        "Your data store is full: its documents use 47 B of the 47 B allowed. "
        "Delete some to upload more."
    )
    assert stored_keys(store_id) == [f"stores/{store_id}/documents/{first['id']}"]
    # Reading and deleting carry on.
    assert [d["id"] for d in api.get("/api/documents").json()] == [first["id"]]
    assert api.delete(f"/api/documents/{first['id']}").status_code == 204

    worker.measure_data_store_usage_task.apply().get()

    assert upload(api, name="more.pdf").status_code == 201


@pytest.mark.parametrize(
    ("n", "says"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (10 * 1024, "10 KB"),
        # Half up, as the Data page's Math.round does.
        (10 * 1024 + 512, "11 KB"),
        (int(2.5 * 1024 * 1024), "2.5 MB"),
        (3 * 1024**3, "3.0 GB"),
        (1024 * 1024 - 1, "1.0 MB"),
        (10 * 1024 - 1, "10 KB"),
    ],
)
def test_sizes_are_written_as_the_data_page_writes_them(n, says):
    assert quota.format_bytes(n) == says


def test_the_api_gives_the_stores_usage_against_both_caps_once_measured(
    api, db, user, files_bucket, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_db_quota_bytes", 100 * 1024**2)
    monkeypatch.setattr(get_settings(), "data_store_files_quota_bytes", 2000)
    store_id = data_store_for(user.id)
    add_document(store_id, b"x" * 1500)
    assert api.get("/api/data-store").json() == {
        "status": "ready",
        "usage": {
            "db_bytes": None,
            "db_quota_bytes": 100 * 1024**2,
            "files_bytes": None,
            "files_quota_bytes": 2000,
            "measured_at": None,
        },
    }

    worker.measure_data_store_usage_task.apply().get()

    usage = api.get("/api/data-store").json()["usage"]
    row = store_row(db, store_id)
    assert usage == {
        "db_bytes": row.db_bytes,
        "db_quota_bytes": 100 * 1024**2,
        "files_bytes": 1500,
        "files_quota_bytes": 2000,
        "measured_at": row.usage_measured_at.isoformat().replace("+00:00", "Z"),
    }
    assert row.db_bytes > 0
