"""Each data store's storage cap (`aios.datastores.quota`): what it uses, measured on a schedule,
against two configured caps, and the writes refused past either."""

import os

import psycopg
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from conftest import add_document
from sqlalchemy import select
from test_data_tools import as_owner
from test_documents import PDF, stored_keys, upload
from test_provisioning import admin_connect

from aios import db_roles, worker
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


def test_the_schedule_records_each_stores_data_and_file_bytes(db, user, files_bucket):
    store = store_row(db, data_store_for(user.id))
    add_document(store.id, b"x" * 1000)
    add_document(store.id, b"y" * 234)
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.notes (id int PRIMARY KEY, body text)")
        conn.execute(f"INSERT INTO data.notes SELECT g, {_KB} FROM generate_series(1, 1000) g")
    assert store_row(db, store.id).usage_measured_at is None

    worker.measure_data_store_usage_task.apply().get()

    row = store_row(db, store.id)
    assert row.files_bytes == 1234
    # The 1 MB of notes, with each row's own few bytes, their index, and the store's own rows.
    assert 1000 * 1024 < row.db_bytes < 1.25 * 1000 * 1024
    assert row.usage_measured_at is not None


def measured(db, store_id) -> int:
    """What the schedule measures the store's database at now."""
    worker.measure_data_store_usage_task.apply().get()
    return store_row(db, store_id).db_bytes


def database_size(store: DataStore) -> int:
    with as_owner(store) as conn:
        return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


# 1 KB of text that doesn't compress (32 md5s), for row `g`.
_KB = "(SELECT string_agg(md5(g::text || '-' || h), '') FROM generate_series(1, 32) h)"


def test_deleting_rows_brings_the_measured_database_down_to_whats_left(db, user):
    """The cap is on the data the store holds: `pg_database_size` keeps the space deleted rows
    took until their table is rewritten, so a store at its cap would stay there."""
    store = store_row(db, data_store_for(user.id))
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.notes (id int PRIMARY KEY, body text)")
    empty = measured(db, store.id)
    with as_owner(store) as conn:
        conn.execute(f"INSERT INTO data.notes SELECT g, {_KB} FROM generate_series(1, 8000) g")
    full = measured(db, store.id)
    size_full = database_size(store)
    assert full - empty > 8000 * 1024

    # Every eighth row stays, all through the table (so no page at its end is left empty for
    # vacuum to give back).
    with as_owner(store) as conn:
        conn.execute("DELETE FROM data.notes WHERE id % 8 <> 0")
    left = measured(db, store.id)

    assert database_size(store) >= size_full
    assert 0.1 < (left - empty) / (full - empty) < 0.16


def test_large_values_kept_out_of_the_table_count_at_their_size(db, user):
    """Postgres keeps values over about 2 KB out of the table's own pages (TOAST), compressed
    if they compress: they count as what they take there."""
    store = store_row(db, data_store_for(user.id))
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.letters (id int PRIMARY KEY, body text, meta jsonb)")
    empty = measured(db, store.id)

    # 30 rows, each with 100 KB of text that doesn't compress, and the same again as jsonb.
    with as_owner(store) as conn:
        conn.execute(
            "INSERT INTO data.letters SELECT g, body, to_jsonb(body) FROM (SELECT g,"
            " (SELECT string_agg(md5(g::text || '-' || h), '') FROM generate_series(1, 3200) h)"
            " AS body FROM generate_series(1, 30) g) s"
        )
    grown = measured(db, store.id) - empty

    assert 0.9 < grown / (30 * 2 * 100 * 1024) < 1.2


def test_the_roles_command_brings_measuring_to_stores_made_before_it(db, user):
    """Stores get pgstattuple from the template, which had none before. Only the master user
    may create it, so `python -m aios.db_roles`, run before every deploy's migrations, adds it
    to the stores that lack it."""
    store = store_row(db, data_store_for(user.id))
    with admin_connect(store.dbname) as admin:
        admin.execute("DROP EXTENSION pgstattuple")
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.notes (id int PRIMARY KEY, body text)")
        conn.execute(f"INSERT INTO data.notes SELECT g, {_KB} FROM generate_series(1, 1000) g")

    settings = get_settings()
    db_roles.ensure_roles(
        os.environ["DATABASE_ADMIN_URL"],
        settings.database_url,
        settings.data_store_provisioner_url,
    )

    assert measured(db, store.id) > 1000 * 1024


def test_a_store_without_pgstattuple_is_measured_by_its_databases_size(api, db, user, queued):
    """One the roles command couldn't reach (see `test_db_roles`): its cap still holds."""
    store = store_row(db, data_store_for(user.id))
    with admin_connect(store.dbname) as admin:
        admin.execute("DROP EXTENSION pgstattuple")

    assert measured(db, store.id) == pytest.approx(database_size(store), abs=1024 * 1024)
    assert upload(api).status_code == 201


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
    """Checked as the upload comes in, not against the last measurement: a store not yet
    measured, or filled since, is held to its cap all the same."""
    monkeypatch.setattr(get_settings(), "data_store_files_quota_bytes", len(PDF))
    store_id = data_store_for(user.id)
    first = upload(api).json()

    r = upload(api, name="more.pdf")

    assert r.status_code == 507
    assert r.json()["detail"] == (
        "Your data store is full: its documents use 47 B of the 47 B allowed. "
        "Delete some to make room."
    )
    assert stored_keys(store_id) == [f"stores/{store_id}/documents/{first['id']}"]
    # Reading and deleting carry on, and a delete makes room straight away.
    assert [d["id"] for d in api.get("/api/documents").json()] == [first["id"]]
    assert api.delete(f"/api/documents/{first['id']}").status_code == 204
    assert upload(api, name="more.pdf").status_code == 201


def test_an_upload_that_would_take_the_documents_past_their_cap_is_refused(
    api, user, queued, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_files_quota_bytes", 70)
    assert upload(api).status_code == 201

    r = upload(api, name="more.pdf")

    assert r.status_code == 507
    assert r.json()["detail"] == (
        "There's no room for more.pdf: your documents use 47 B of the 70 B allowed, and it's"
        " 47 B. Delete some to make room."
    )
    assert upload(api, name="small.txt", data=b"x" * 23).status_code == 201


def test_a_store_at_its_database_cap_refuses_uploads(api, db, user, queued, monkeypatch):
    """Ingesting a document writes its chunks to the store's database."""
    monkeypatch.setattr(get_settings(), "data_store_db_quota_bytes", 1024)
    store_id = data_store_for(user.id)
    with as_owner(store_row(db, store_id)) as conn:
        conn.execute("CREATE TABLE data.notes (id int PRIMARY KEY, body text)")
        conn.execute(f"INSERT INTO data.notes SELECT g, {_KB} FROM generate_series(1, 10) g")

    r = upload(api)

    assert r.status_code == 507
    assert r.json()["detail"].startswith("Your data store is full: its database uses ")
    assert r.json()["detail"].endswith(" of the 1.0 KB allowed, so documents can't be uploaded.")
    assert stored_keys(store_id) == []
    assert queued == []


def test_uploads_and_deletes_record_the_documents_bytes_straight_away(
    api, user, queued, monkeypatch
):
    """So the Data page sees room made (or taken) without waiting for the schedule."""
    first = upload(api).json()
    upload(api, name="more.pdf", data=b"x" * 3)
    assert api.get("/api/data-store").json()["usage"]["files_bytes"] == 50

    api.delete(f"/api/documents/{first['id']}")

    assert api.get("/api/data-store").json()["usage"]["files_bytes"] == 3


@pytest.mark.parametrize(
    ("n", "says"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (10 * 1024, "10 KB"),
        # Half up, as the Data page's Math.round and toFixed do.
        (10 * 1024 + 512, "11 KB"),
        (1280, "1.3 KB"),
        (int(2.25 * 1024**3), "2.3 GB"),
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


def test_measuring_leaves_no_connection_open_on_the_stores(db, user):
    """The provisioning worker measures every store, and would otherwise keep a connection to
    each (up to the engines it caches) between runs."""
    row = store_row(db, data_store_for(user.id))

    worker.measure_data_store_usage_task.apply().get()

    with admin_connect() as admin:
        held = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
            (f"{row.role_prefix}_ro",),
        ).fetchone()[0]
    assert held == 0


def test_a_run_past_its_time_limit_stops_rather_than_measure_on(db, user, new_user, monkeypatch):
    """The soft time limit raises in the task, which must not be taken for one store failing:
    the next run, due by then, carries on."""
    provision.provision(data_store_for(new_user().id))
    measured = []

    def measure(store_id):
        measured.append(store_id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(quota, "measure", measure)

    with pytest.raises(SoftTimeLimitExceeded):
        quota.measure_all()
    assert len(measured) == 1
