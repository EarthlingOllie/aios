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
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from conftest import delete_user
from psycopg import errors, sql
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import make_url

from aios import worker
from aios.auth import current_user
from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, data_store_for, migrations, provision, store_session
from aios.datastores.connections import store_url
from aios.datastores.migrations import head_revision
from aios.datastores.resolve import names_for
from aios.db import SessionLocal
from aios.main import app
from aios.models import DataStore, DataStoreStatus, User


def store_row(db, store_id: uuid.UUID) -> DataStore:
    db.expire_all()
    return db.scalar(select(DataStore).where(DataStore.id == store_id))


@pytest.fixture
def new_user(db) -> Iterator[Callable[[], User]]:
    """Makes users with no store yet; deletes them (and drops their stores) afterwards."""
    made: list[User] = []

    def make() -> User:
        u = User(github_id=-(1000 + len(made)), login=f"new-user-{len(made)}")
        db.add(u)
        db.commit()
        made.append(u)
        return u

    yield make
    for u in made:
        delete_user(db, u)


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


def test_a_store_is_reached_at_the_platform_databases_address_not_the_provisioners(
    db, new_user, monkeypatch
):
    """The provisioner may know the instance by a name the app can't resolve (Compose's `db`,
    with the API on the host): the app reaches a store where it reaches the platform database."""
    settings = get_settings()
    prov = make_url(settings.data_store_provisioner_url)
    # A name only the provisioner can resolve: libpq connects to `hostaddr` instead.
    as_db = prov.set(host="db.invalid").update_query_dict({"hostaddr": "127.0.0.1"})
    monkeypatch.setattr(
        settings, "data_store_provisioner_url", as_db.render_as_string(hide_password=False)
    )
    store_id = data_store_for(new_user().id)

    provision.provision(store_id)

    with store_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT current_database()")) == store_row(db, store_id).dbname


def test_a_store_moved_to_another_instance_is_reached_there_with_the_apps_options(monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "database_url",
        "postgresql+psycopg://app:pw@platform:5432/aios?sslmode=require",
    )
    store = DataStore(
        dbname="ds_x",
        role_prefix="ds_x",
        rw_password=credentials.encrypt("rw-secret"),
        ro_password=credentials.encrypt("ro-secret"),
    )
    here = store_url(store, "ro")
    store.host, store.port = "elsewhere", 6543
    moved = store_url(store, "ro")

    assert (here.host, here.port, here.database, here.username, here.password) == (
        "platform",
        5432,
        "ds_x",
        "ds_x_ro",
        "ro-secret",
    )
    assert (moved.host, moved.port) == ("elsewhere", 6543)
    assert here.query == moved.query == {"sslmode": "require"}


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
    return (
        store_url(store, role)
        .set(drivername="postgresql", database=database or store.dbname)
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


def test_no_other_role_can_connect_to_a_new_store_before_its_access_is_set(
    db, new_user, two_stores, monkeypatch
):
    """A new database lets every role connect, until `_restrict_connect` says who may: one let
    in before then would stay connected."""
    other, _ = two_stores
    store_id = data_store_for(new_user().id)
    steps = list(provision.STEPS)
    monkeypatch.setattr(provision, "STEPS", steps[: steps.index(provision._create_database) + 1])
    provision.provision(store_id)
    dbname = store_row(db, store_id).dbname

    for role in ("rw", "ro"):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(login_url(other, role, dbname)).close()

    monkeypatch.setattr(provision, "STEPS", steps)
    provision.provision(store_id)
    psycopg.connect(login_url(store_row(db, store_id), "rw")).close()


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


@pytest.fixture
def store_with_a_table(db, new_user, next_revision) -> DataStore:
    """A store provisioned at `next_revision`, whose `aios.marker` is a table like any a store
    revision makes."""
    next_revision()
    store_id = data_store_for(new_user().id)
    provision.provision(store_id)
    return store_row(db, store_id)


def test_a_stores_read_only_role_cant_write(store_with_a_table):
    a = store_with_a_table
    insert = "INSERT INTO aios.marker VALUES (1)"
    with psycopg.connect(login_url(a, "ro"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM aios.marker").fetchone() == (0,)
        for write in (insert, "DELETE FROM aios.marker", "TRUNCATE aios.marker"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(write)
    # The read-write role can.
    with psycopg.connect(login_url(a, "rw"), autocommit=True) as conn:
        conn.execute(insert)
        assert conn.execute("UPDATE aios.marker SET id = 2").rowcount == 1
        assert conn.execute("DELETE FROM aios.marker").rowcount == 1


# The platform's own record of the store's schema: only its migrations (as the owner) change it.
BOOKKEEPING_WRITES = [
    "INSERT INTO aios.alembic_version VALUES ('x')",
    "UPDATE aios.alembic_version SET version_num = 'x'",
    "DELETE FROM aios.alembic_version",
    "TRUNCATE aios.alembic_version",
    "INSERT INTO aios.schema_history (summary, sql) VALUES ('x', 'x')",
    "UPDATE aios.schema_history SET summary = 'x'",
    "DELETE FROM aios.schema_history",
    "TRUNCATE aios.schema_history",
]


def assert_cant_change_bookkeeping(store: DataStore, role: str) -> None:
    with psycopg.connect(login_url(store, role), autocommit=True) as conn:
        # But may read it.
        assert conn.execute("SELECT version_num FROM aios.alembic_version").fetchone()
        conn.execute("SELECT count(*) FROM aios.schema_history")
        for write in BOOKKEEPING_WRITES:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(write)


@pytest.mark.parametrize("role", ["rw", "ro"])
def test_a_stores_roles_cant_change_its_migration_version_or_schema_history(two_stores, role):
    assert_cant_change_bookkeeping(two_stores[0], role)


def test_migrate_all_takes_writes_on_the_bookkeeping_back_from_existing_stores(db, new_user):
    """Stores provisioned before `_rw` was kept off the bookkeeping."""
    store_id = data_store_for(new_user().id)
    provision.provision(store_id)
    row = store_row(db, store_id)
    with admin_connect(row.dbname) as conn:
        conn.execute(
            sql.SQL(
                "GRANT INSERT, UPDATE, DELETE ON aios.alembic_version, aios.schema_history TO {}"
            ).format(sql.Identifier(f"{row.role_prefix}_rw"))
        )

    assert provision.migrate_all([store_id]) == {store_id: head_revision()}

    assert_cant_change_bookkeeping(row, "rw")


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
    db, new_user, monkeypatch, next_revision, crash_after
):
    next_revision()  # a table for the read-write role to write
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
        conn.execute("INSERT INTO aios.marker VALUES (1)")
    assert_cant_change_bookkeeping(row, "rw")


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
down_revision = "0001"
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
        (tmp_path / "t0002_marker.py").write_text(NEXT_REVISION)
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
    head = next_revision()
    provision.migrate_store(ahead)
    # The broken store is first in line, and can't be migrated: the new revision's table is
    # already there.
    with admin_connect(store_row(db, broken).dbname) as conn:
        conn.execute("CREATE TABLE aios.marker (id int)")
    assert [store_row(db, s).schema_revision for s in ids] == ["0001", "0001", head]

    outcome = provision.migrate_all(ids)

    assert {s: outcome[s] for s in (behind, ahead)} == {behind: head, ahead: head}
    assert isinstance(outcome[broken], Exception)
    rows = {s: store_row(db, s) for s in ids}
    assert [rows[s].schema_revision for s in ids] == ["0001", head, head]
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


def test_a_store_the_release_before_databases_makes_gets_its_names_and_is_provisioned(db, new_user):
    """While a deploy migrates, the release before migration 0012 still serves, and makes a
    new user's store with only its owner: the row gets its names from its id."""
    user = new_user()
    store_id = db.scalar(insert(DataStore).values(owner_user_id=user.id).returning(DataStore.id))
    db.commit()

    row = store_row(db, store_id)
    assert (row.dbname, row.role_prefix) == names_for(store_id)
    assert row.status == DataStoreStatus.provisioning

    provision.migrate_all([store_id])

    row = store_row(db, store_id)
    assert row.status == DataStoreStatus.ready
    with psycopg.connect(login_url(row, "rw")) as conn:
        assert conn.execute("SELECT current_database()").fetchone() == (row.dbname,)


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
    gave_up, lost, just_signed_up = [data_store_for(new_user().id) for _ in range(3)]
    provision.fail(gave_up, "OperationalError: the instance is down")
    # Its task was never queued, or was lost with a worker that was killed (the broker only
    # redelivers that after its visibility timeout, over an hour): it needn't wait for that.
    db.execute(
        update(DataStore)
        .where(DataStore.id == lost)
        .values(created_at=text("now() - interval '10 minutes'"))
    )
    db.commit()
    # Only this test's stores: others in the database are left alone.
    retry = provision.retry_unfinished
    monkeypatch.setattr(
        provision, "retry_unfinished", lambda: retry([gave_up, lost, just_signed_up])
    )

    worker.retry_unfinished_data_stores_task.apply().get()

    assert store_row(db, gave_up).status == DataStoreStatus.ready
    assert store_row(db, gave_up).error is None
    assert store_row(db, lost).status == DataStoreStatus.ready
    # Its own provisioning task may still be trying: it's left to that.
    assert store_row(db, just_signed_up).status == DataStoreStatus.provisioning
    # The provisioning worker runs it (`celery worker -B`), as the provisioner.
    scheduled = [e for e in worker.celery_app.conf.beat_schedule.values()]
    assert [e["task"] for e in scheduled] == ["aios.retry_unfinished_data_stores"]
    assert worker.celery_app.conf.task_routes["aios.retry_unfinished_data_stores"] == {
        "queue": "provisioning"
    }


def test_runs_of_a_store_at_once_wait_for_each_other(db, new_user):
    """So the sweep can provision a store whose own task is running, or about to."""
    store_id = data_store_for(new_user().id)

    with ThreadPoolExecutor(2) as pool:
        revisions = list(pool.map(provision.provision, [store_id, store_id]))

    # One provisioned it; the other found it ready and left it.
    assert sorted(revisions, key=str) == [head_revision(), None]
    assert store_row(db, store_id).status == DataStoreStatus.ready


def test_a_time_limit_stops_retrying_stores_without_failing_the_one_it_cut_short(
    db, new_user, monkeypatch
):
    """The provisioning worker's time limits stop the whole run, not just one store's."""
    stores = [data_store_for(new_user().id) for _ in range(2)]
    for store_id in stores:
        provision.fail(store_id, "OperationalError: the instance is down")
    tries = []

    def out_of_time(store_id):
        tries.append(store_id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(provision, "provision", out_of_time)

    with pytest.raises(SoftTimeLimitExceeded):
        provision.retry_unfinished(stores)

    assert len(tries) == 1
    for store_id in stores:
        row = store_row(db, store_id)
        assert (row.status, row.error) == (
            DataStoreStatus.failed,
            "OperationalError: the instance is down",
        )


def test_a_time_limit_stops_migrating_stores_without_recording_an_error(db, new_user, monkeypatch):
    stores = [data_store_for(new_user().id) for _ in range(2)]
    for store_id in stores:
        provision.provision(store_id)
    tries = []

    def out_of_time(store_id):
        tries.append(store_id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(provision, "migrate_store", out_of_time)

    with pytest.raises(SoftTimeLimitExceeded):
        provision.migrate_all(stores)

    assert len(tries) == 1
    assert [store_row(db, s).error for s in stores] == [None, None]


def test_provisioning_tasks_have_time_limits_of_their_own():
    """Not ingest's half hour: provisioning a store takes seconds."""
    for task in (worker.provision_data_store_task, worker.retry_unfinished_data_stores_task):
        assert task.soft_time_limit < task.time_limit < get_settings().ingest_time_limit


def test_running_migrate_all_as_a_module_gives_no_import_warning():
    """`python -m` runs a fresh copy of the module; one imported before it is a second copy."""
    result = subprocess.run(
        [sys.executable, "-m", "aios.datastores.provision"],
        cwd=BACKEND,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2  # usage: no command given
    assert "RuntimeWarning" not in result.stderr
