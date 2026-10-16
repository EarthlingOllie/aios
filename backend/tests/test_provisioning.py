"""Provisioning a data store: a database of its own on the shared instance (`aios.datastores`).

These create real databases and roles, as the provisioner (DATA_STORE_PROVISIONER_URL), and
drop them afterwards.
"""

import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url

from aios import worker
from aios.auth import current_user
from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, data_store_for, migrations, provision, store_session
from aios.datastores.migrations import head_revision
from aios.db import SessionLocal
from aios.main import app
from aios.models import DataStore, DataStoreStatus


def store_row(db, store_id: uuid.UUID) -> DataStore:
    db.expire_all()
    return db.scalar(select(DataStore).where(DataStore.id == store_id))


def test_a_new_users_store_is_provisioned_ready_at_the_store_trees_head(db, new_user, provisioning):
    store_id = data_store_for(new_user().id)

    assert store_row(db, store_id).status == DataStoreStatus.provisioning
    assert provisioning == [store_id]

    provision.provision(store_id)

    row = store_row(db, store_id)
    assert row.status == DataStoreStatus.ready
    assert row.schema_revision == head_revision()
    with store_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT count(*) FROM aios.schema_history")) == 0
        assert store.scalar(text("SELECT current_database()")) == row.dbname


@pytest.fixture
def two_stores(db, new_user) -> tuple[DataStore, DataStore]:
    """Two provisioned stores, A and B."""
    rows = []
    for _ in range(2):
        store_id = data_store_for(new_user().id)
        provision.provision(store_id)
        rows.append(store_row(db, store_id))
    return rows[0], rows[1]


def login_url(store: DataStore, role: str, database: str | None = None) -> str:
    """Store `store`'s `role`, as the app connects as it, to `database` (by default its own)."""
    password = credentials.decrypt(store.rw_password if role == "rw" else store.ro_password)
    return (
        make_url(get_settings().database_url)
        .set(
            drivername="postgresql",
            username=f"{store.role_prefix}_{role}",
            password=password,
            host=store.host,
            port=store.port,
            database=database or store.dbname,
        )
        .render_as_string(hide_password=False)
    )


@pytest.mark.parametrize("role", ["rw", "ro"])
def test_a_stores_roles_cant_connect_to_another_store_or_the_platform(two_stores, role):
    a, b = two_stores
    platform = make_url(get_settings().database_url).database
    template = get_settings().data_store_template
    for database in (b.dbname, platform, "postgres", "template1", template):
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(login_url(a, role, database)).close()
    # But it can connect to its own.
    psycopg.connect(login_url(a, role)).close()


@pytest.mark.parametrize("role", ["rw", "ro"])
def test_a_stores_roles_cant_create_anything(two_stores, role):
    a, _ = two_stores
    with psycopg.connect(login_url(a, role), autocommit=True) as conn:
        schemas = [s for (s,) in conn.execute("SELECT nspname FROM pg_namespace")]
        assert {"aios", "extensions", "pg_catalog", "information_schema"} <= set(schemas)
        assert "public" not in schemas
        for schema in schemas:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(sql.SQL("CREATE TABLE {}.t (i int)").format(sql.Identifier(schema)))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE SCHEMA mine")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE TEMPORARY TABLE t (i int)")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE EXTENSION IF NOT EXISTS dblink")


def test_a_stores_read_only_role_cant_write(two_stores):
    a, _ = two_stores
    insert = "INSERT INTO aios.schema_history (summary, sql) VALUES ('x', 'x')"
    with psycopg.connect(login_url(a, "ro"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM aios.schema_history").fetchone() == (0,)
        for write in (insert, "DELETE FROM aios.schema_history", "TRUNCATE aios.schema_history"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(write)
    # The read-write role can.
    with psycopg.connect(login_url(a, "rw"), autocommit=True) as conn:
        conn.execute(insert)
        assert conn.execute("DELETE FROM aios.schema_history").rowcount == 1


def test_a_stores_roles_have_their_limits(two_stores):
    a, _ = two_stores
    settings = get_settings()
    for role in ("rw", "ro"):
        with psycopg.connect(login_url(a, role), autocommit=True) as conn:
            timeouts = conn.execute(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('statement_timeout', 'idle_in_transaction_session_timeout')"
            )
            # In milliseconds.
            assert dict(timeouts.fetchall()) == {
                "statement_timeout": "30000",
                "idle_in_transaction_session_timeout": "60000",
            }
            limit = conn.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user")
            assert limit.fetchone() == (settings.data_store_connection_limit,)
            # pgvector works without naming the schema it's in.
            distance = conn.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
            assert distance.fetchone() == (1.0,)


class Crash(Exception):
    pass


@pytest.mark.parametrize("crash_after", range(len(provision.STEPS) - 1))
def test_a_run_that_crashes_partway_is_finished_by_running_it_again(
    db, new_user, monkeypatch, crash_after
):
    store_id = data_store_for(new_user().id)
    steps = list(provision.STEPS)

    def crash(run):
        steps[crash_after](run)
        raise Crash

    monkeypatch.setattr(provision, "STEPS", [*steps[:crash_after], crash])
    with pytest.raises(Crash):
        provision.provision(store_id)
    assert store_row(db, store_id).status == DataStoreStatus.provisioning

    monkeypatch.setattr(provision, "STEPS", steps)
    provision.provision(store_id)

    row = store_row(db, store_id)
    assert (row.status, row.schema_revision) == (DataStoreStatus.ready, head_revision())
    with psycopg.connect(login_url(row, "rw"), autocommit=True) as conn:
        conn.execute("INSERT INTO aios.schema_history (summary, sql) VALUES ('x', 'x')")


def admin_connect(database: str | None = None) -> psycopg.Connection:
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("DATABASE_ADMIN_URL not set")
    parts = {"database": database} if database else {}
    return psycopg.connect(libpq_conninfo(url, **parts), autocommit=True)


def test_killing_the_provisioning_process_then_running_it_again_gives_a_ready_store(db, new_user):
    store_id = data_store_for(new_user().id)
    dbname = store_row(db, store_id).dbname
    # Hold the store's row, so the run stops at its very last step (marking the store ready),
    # with everything else done; then kill it there.
    with SessionLocal() as holder:
        holder.execute(select(DataStore).where(DataStore.id == store_id).with_for_update())
        proc = subprocess.Popen(
            [sys.executable, "-c", PROVISION.format(store_id=store_id)],
            cwd=BACKEND,
            env=os.environ.copy(),
        )
        try:
            deadline = time.monotonic() + 30
            with admin_connect() as admin:
                while not admin.execute(WAITING_TO_MARK_READY).fetchone():
                    assert proc.poll() is None, "provisioning exited early"
                    assert time.monotonic() < deadline, "provisioning never got to the end"
                    time.sleep(0.05)
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        holder.rollback()

    assert store_row(db, store_id).status == DataStoreStatus.provisioning
    with admin_connect(dbname) as conn:
        assert conn.execute("SELECT version_num FROM aios.alembic_version").fetchone()

    provision.provision(store_id)

    row = store_row(db, store_id)
    assert (row.status, row.schema_revision) == (DataStoreStatus.ready, head_revision())
    with store_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT count(*) FROM aios.schema_history")) == 0


BACKEND = Path(__file__).resolve().parents[1]
PROVISION = (
    "import uuid; from aios.datastores import provision; "
    "provision.provision(uuid.UUID('{store_id}'))"
)
# The provisioning process waiting for the store's row, to mark it ready.
WAITING_TO_MARK_READY = (
    "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
    "AND query ILIKE 'UPDATE data_stores%'"
)


def test_database_and_role_names_never_contain_a_user_id(two_stores):
    for store in two_stores:
        hex_id = store.id.hex
        names = provision.StoreNames.of(store)
        assert names == provision.StoreNames(
            f"ds_{hex_id}", f"ds_{hex_id}_owner", f"ds_{hex_id}_rw", f"ds_{hex_id}_ro"
        )
        with psycopg.connect(login_url(store, "ro"), autocommit=True) as conn:
            roles = {r for (r,) in conn.execute("SELECT rolname FROM pg_roles")}
            assert conn.execute("SELECT current_database()").fetchone() == (names.dbname,)
        assert {names.owner, names.rw, names.ro} <= roles
        assert store.owner_user_id != 0 and not re.search(
            rf"(^|_){store.owner_user_id}(_|$)", " ".join(roles)
        )


# A second revision of the store tree, for the migration tests.
NEXT_REVISION = """
from alembic import op
import sqlalchemy as sa

revision = "t0002"
down_revision = "{base}"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("marker", sa.Column("id", sa.Integer, primary_key=True), schema="aios")


def downgrade():
    op.drop_table("marker", schema="aios")
"""


@pytest.fixture
def next_revision(tmp_path, monkeypatch) -> Callable[[], str]:
    """Adds `t0002` to the store tree when called; returns its id."""

    def add() -> str:
        (tmp_path / "t0002_marker.py").write_text(NEXT_REVISION.format(base=head_revision()))
        monkeypatch.setattr(
            migrations, "VERSION_LOCATIONS", [*migrations.VERSION_LOCATIONS, str(tmp_path)]
        )
        return "t0002"

    return add


def test_migrate_all_brings_stores_at_mixed_revisions_to_head_past_one_that_fails(
    db, new_user, next_revision
):
    ids = [data_store_for(new_user().id) for _ in range(3)]
    for store_id in ids:
        provision.provision(store_id)
    broken, behind, ahead = ids
    base = head_revision()
    head = next_revision()
    provision.migrate_store(ahead)
    # The broken store is first in line, and can't be migrated: the new revision's table is
    # already there.
    with admin_connect(store_row(db, broken).dbname) as conn:
        conn.execute("CREATE TABLE aios.marker (id int)")
    assert [store_row(db, s).schema_revision for s in ids] == [base, base, head]

    outcome = provision.migrate_all(ids)

    assert {s: outcome[s] for s in (behind, ahead)} == {behind: head, ahead: head}
    assert isinstance(outcome[broken], Exception)
    rows = {s: store_row(db, s) for s in ids}
    assert [rows[s].schema_revision for s in ids] == [base, head, head]
    assert "marker" in rows[broken].error
    assert all(r.status == DataStoreStatus.ready for r in rows.values())
    with store_session(behind, "ro") as store:
        assert store.scalar(text("SELECT count(*) FROM aios.marker")) == 0


def test_migrate_all_reports_failures_and_exits_non_zero(
    db, new_user, next_revision, capsys, monkeypatch
):
    ok, broken = [data_store_for(new_user().id) for _ in range(2)]
    for store_id in (ok, broken):
        provision.provision(store_id)
    next_revision()
    # Only this test's stores: others in the database mustn't get the test's revision.
    migrate_all = provision.migrate_all
    monkeypatch.setattr(provision, "migrate_all", lambda: migrate_all([ok, broken]))
    with admin_connect(store_row(db, broken).dbname) as conn:
        conn.execute("CREATE TABLE aios.marker (id int)")

    assert provision.main(["migrate-all"]) == 1

    out = capsys.readouterr().out
    assert f"{ok}: t0002" in out
    assert f"{broken}: " in out and "marker" in out


def test_migrate_all_finishes_provisioning_stores_that_never_got_there(db, new_user):
    store_id = data_store_for(new_user().id)  # queued, but never run

    outcome = provision.migrate_all([store_id])

    assert outcome[store_id] == head_revision()
    assert store_row(db, store_id).status == DataStoreStatus.ready


def test_migrate_all_runs_each_store_to_head_once(db, new_user, monkeypatch):
    """A store it has just provisioned is already at head: it isn't migrated again."""
    unfinished = data_store_for(new_user().id)
    ready = data_store_for(new_user().id)
    provision.provision(ready)
    upgrades = []
    upgrade_store = provision.upgrade_store

    def counted(names):
        upgrades.append(names.dbname)
        return upgrade_store(names)

    monkeypatch.setattr(provision, "upgrade_store", counted)

    outcome = provision.migrate_all([unfinished, ready])

    assert outcome == {unfinished: head_revision(), ready: head_revision()}
    assert sorted(upgrades) == sorted(store_row(db, s).dbname for s in (unfinished, ready))


def test_a_store_from_before_databases_gets_its_passwords_when_provisioned(db, new_user):
    """Stores made before migration 0012 have no passwords yet."""
    store_id = data_store_for(new_user().id)
    db.execute(
        update(DataStore).where(DataStore.id == store_id).values(rw_password=None, ro_password=None)
    )
    db.commit()

    provision.migrate_all([store_id])

    row = store_row(db, store_id)
    assert row.status == DataStoreStatus.ready
    with psycopg.connect(login_url(row, "rw")) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (f"{row.role_prefix}_rw",)


def test_store_passwords_are_kept_encrypted_and_never_logged_or_returned(
    db, new_user, caplog, client
):
    caplog.set_level(logging.DEBUG)
    user = new_user()
    store_id = data_store_for(user.id)
    provision.provision(store_id)
    provision.migrate_all([store_id])
    row = store_row(db, store_id)
    passwords = [credentials.decrypt(row.rw_password), credentials.decrypt(row.ro_password)]

    # Encrypted on the row, and they log in.
    for role, password in zip(("rw", "ro"), passwords, strict=True):
        assert password not in row.rw_password + row.ro_password
        psycopg.connect(login_url(row, role)).close()
    # The API says only how the store is.
    app.dependency_overrides[current_user] = lambda: user
    try:
        response = client.get("/api/data-store")
    finally:
        app.dependency_overrides.clear()
    assert response.json() == {"status": "ready"}
    for password in passwords:
        assert password not in caplog.text
        assert password not in response.text


def test_the_provisioning_task_provisions_the_store_it_is_given(db, new_user):
    store_id = data_store_for(new_user().id)

    worker.provision_data_store_task.apply(args=[str(store_id)]).get()

    assert store_row(db, store_id).status == DataStoreStatus.ready


def test_the_provisioning_task_retries_then_marks_the_store_failed(db, new_user, monkeypatch):
    store_id = data_store_for(new_user().id)
    tries = []

    def broken(store_id):
        tries.append(store_id)
        raise RuntimeError("the template is missing")

    monkeypatch.setattr(provision, "provision", broken)

    result = worker.provision_data_store_task.apply(args=[str(store_id)])

    assert result.failed()
    assert len(tries) == worker.PROVISION_RETRIES + 1
    row = store_row(db, store_id)
    assert row.status == DataStoreStatus.failed
    assert row.error == "RuntimeError: the template is missing"

    # The deploy's migrate-all tries again.
    monkeypatch.undo()
    assert provision.migrate_all([store_id])[store_id] == head_revision()
    assert store_row(db, store_id).status == DataStoreStatus.ready


def test_stores_that_never_got_there_are_tried_again_on_a_schedule(db, new_user, monkeypatch):
    gave_up, never_queued, just_signed_up = [data_store_for(new_user().id) for _ in range(3)]
    provision.fail(gave_up, "OperationalError: the instance is down")
    db.execute(
        update(DataStore)
        .where(DataStore.id == never_queued)
        .values(created_at=text("now() - interval '2 hours'"))
    )
    db.commit()
    # Only this test's stores: others in the database are left alone.
    retry = provision.retry_unfinished
    monkeypatch.setattr(
        provision, "retry_unfinished", lambda: retry([gave_up, never_queued, just_signed_up])
    )

    worker.retry_unfinished_data_stores_task.apply().get()

    assert store_row(db, gave_up).status == DataStoreStatus.ready
    assert store_row(db, gave_up).error is None
    assert store_row(db, never_queued).status == DataStoreStatus.ready
    # Its own provisioning task may still be trying: it's left to that.
    assert store_row(db, just_signed_up).status == DataStoreStatus.provisioning
    # The provisioning worker runs it (`celery worker -B`), as the provisioner.
    scheduled = [e for e in worker.celery_app.conf.beat_schedule.values()]
    assert [e["task"] for e in scheduled] == ["aios.retry_unfinished_data_stores"]
    assert worker.celery_app.conf.task_routes["aios.retry_unfinished_data_stores"] == {
        "queue": "provisioning"
    }
