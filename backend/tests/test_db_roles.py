"""The platform database's roles (`aios.db_roles`): the app's least-privilege role and the
provisioner's.

These run the bootstrap against a throwaway database, with role names of their own, so they
don't touch the real `aios_app` and `aios_provisioner`. They need the master user's DSN
(DATABASE_ADMIN_URL) and skip without it.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aios import checkpoints
from aios.config import get_settings, libpq_conninfo
from aios.db_roles import ensure_roles

BACKEND = Path(__file__).resolve().parents[1]


def _connect(url: str, **parts) -> psycopg.Connection:
    return psycopg.connect(libpq_conninfo(url, **parts), autocommit=True)


@pytest.fixture
def admin_url() -> str:
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        pytest.skip("DATABASE_ADMIN_URL not set")
    try:
        _connect(url).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"master user can't connect: {exc}")
    return url


@pytest.fixture
def cluster(admin_url):
    """A throwaway database owned by the master user, and names for its two roles.

    Yields (admin_url, app_url, provisioner_url), each pointing at that database. The master
    user needn't be a superuser (on RDS it isn't), as long as pgvector is already installed or
    it may install it.
    """
    tag = uuid.uuid4().hex[:8]
    dbname, app, prov = f"t_roles_{tag}", f"t_app_{tag}", f"t_prov_{tag}"
    with _connect(admin_url) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    base = make_url(admin_url)
    app_url = base.set(username=app, password=f"pw-{app}", database=dbname)
    prov_url = base.set(username=prov, password=f"pw-{prov}", database=dbname)
    try:
        yield (
            base.set(database=dbname).render_as_string(hide_password=False),
            app_url.render_as_string(hide_password=False),
            prov_url.render_as_string(hide_password=False),
        )
    finally:
        with _connect(admin_url) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            for role in (app, prov):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_app_role_cannot_create_databases_or_roles(cluster):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)

    with _connect(app_url) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE DATABASE t_app_may_not")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE t_app_may_not")


def test_app_role_cannot_drop_or_reconfigure_the_platform_database(cluster):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)
    db = sql.Identifier(make_url(app_url).database)

    with _connect(app_url) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(sql.SQL("ALTER DATABASE {} SET statement_timeout = 0").format(db))
    with (
        _connect(app_url, database="postgres") as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(sql.SQL("DROP DATABASE {}").format(db))


def test_provisioner_role_can_create_databases_and_roles(cluster):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)
    tag = uuid.uuid4().hex[:8]
    db, role = sql.Identifier(f"t_prov_db_{tag}"), sql.Identifier(f"t_prov_role_{tag}")

    with _connect(prov_url) as conn:
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(db))
            conn.execute(sql.SQL("CREATE ROLE {}").format(role))
        finally:
            # As the provisioner: a master user that isn't a superuser couldn't drop them.
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(db))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(role))


def test_the_app_connects_as_a_role_that_cannot_create_databases_or_roles(db):
    """DATABASE_URL, which the API, the workers and migrations use (and so does this suite)."""
    row = db.execute(
        text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
    ).one()

    assert tuple(row) == (False, False, False)


def test_db_tests_fail_rather_than_skip_when_the_app_role_cannot_log_in(admin_url):
    """A server that's up but rejects DATABASE_URL's role (the roles were never created) is a
    setup to fix, not a database that isn't there."""
    nobody = make_url(admin_url).set(username="t_no_such_role", password="x")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_db_roles.py::"
            "test_the_app_connects_as_a_role_that_cannot_create_databases_or_roles",
        ],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": nobody.render_as_string(hide_password=False)},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout
    assert "python -m aios.db_roles" in result.stdout


def _bootstrap(**env: str) -> subprocess.CompletedProcess:
    """`python -m aios.db_roles` with just `env` for its settings (and no `.env` file)."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_", "DATA_STORE_"))}
    return subprocess.run(
        [sys.executable, "-m", "aios.db_roles"],
        cwd=BACKEND / "tests",
        env={**base, **env},
        capture_output=True,
        text=True,
    )


def test_command_creates_the_roles_from_the_environment(cluster):
    admin_url, app_url, prov_url = cluster

    result = _bootstrap(
        DATABASE_ADMIN_URL=admin_url, DATABASE_URL=app_url, DATA_STORE_PROVISIONER_URL=prov_url
    )

    assert result.returncode == 0, result.stderr
    with _connect(prov_url) as conn:
        conn.execute("SELECT 1")
    with _connect(app_url) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("CREATE ROLE t_app_may_not")


def test_command_needs_the_master_users_url(cluster):
    _, app_url, prov_url = cluster

    result = _bootstrap(DATABASE_URL=app_url, DATA_STORE_PROVISIONER_URL=prov_url)

    assert result.returncode != 0
    assert "DATABASE_ADMIN_URL" in result.stderr


def test_command_reports_the_setting_that_is_actually_invalid(cluster):
    admin_url, app_url, prov_url = cluster

    result = _bootstrap(
        DATABASE_ADMIN_URL=admin_url,
        DATABASE_URL=app_url,
        DATA_STORE_PROVISIONER_URL=prov_url,
        COOKIE_SECURE="not-a-bool",
    )

    assert result.returncode != 0
    assert "cookie_secure" in result.stderr
    assert "DATABASE_ADMIN_URL" not in result.stderr


def test_rerunning_changes_the_passwords(cluster):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)
    rotated = make_url(app_url).set(password="rotated").render_as_string(hide_password=False)

    ensure_roles(admin_url, rotated, prov_url)

    _connect(rotated).close()
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        _connect(app_url)


def _attrs(admin_url: str, role: str) -> tuple | None:
    with _connect(admin_url) as conn:
        return conn.execute(
            "SELECT rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()


def _as(url: str, role: str) -> str:
    return make_url(url).set(username=role).render_as_string(hide_password=False)


@pytest.mark.parametrize(
    "same", ["app_and_provisioner", "app_and_master", "provisioner_and_master"]
)
def test_refuses_urls_that_name_the_same_role(cluster, same):
    """Else the app's role could end up with CREATEDB, or the master user lose it."""
    admin_url, app_url, prov_url = cluster
    admin = make_url(admin_url).username
    app, prov = make_url(app_url).username, make_url(prov_url).username
    master_before = _attrs(admin_url, admin)
    if same == "app_and_provisioner":
        prov_url = _as(prov_url, app)
    elif same == "app_and_master":
        app_url = _as(app_url, admin)
    else:
        prov_url = _as(prov_url, admin)

    with pytest.raises(ValueError, match="different"):
        ensure_roles(admin_url, app_url, prov_url)

    assert _attrs(admin_url, admin) == master_before
    assert _attrs(admin_url, app) is None
    assert _attrs(admin_url, prov) is None


def test_refuses_a_url_without_a_password(cluster):
    """A missing password would otherwise clear the role's (PASSWORD NULL), locking it out."""
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)
    u = make_url(app_url)
    no_password = f"{u.drivername}://{u.username}@{u.host}:{u.port}/{u.database}"

    with pytest.raises(ValueError, match="password"):
        ensure_roles(admin_url, no_password, prov_url)

    _connect(app_url).close()


@pytest.mark.parametrize("which", ["app", "provisioner"])
@pytest.mark.parametrize("part", [{"host": "elsewhere.invalid"}, {"port": 5433}])
def test_refuses_urls_for_another_server(cluster, which, part):
    """Else the roles are made on the master user's server, and the app can't log in to its own."""
    admin_url, app_url, prov_url = cluster
    moved = lambda url: make_url(url).set(**part).render_as_string(hide_password=False)  # noqa: E731
    if which == "app":
        app_url = moved(app_url)
    else:
        prov_url = moved(prov_url)

    with pytest.raises(ValueError, match="same server"):
        ensure_roles(admin_url, app_url, prov_url)

    assert _attrs(admin_url, make_url(app_url).username) is None


def test_the_same_server_may_be_written_differently(cluster):
    """Host names compare without case, and no port is Postgres's default one."""
    admin_url, app_url, prov_url = cluster
    u = make_url(app_url)
    if u.port not in (None, 5432):
        pytest.skip("the master user's URL isn't on the default port")
    app_url = u.set(host=u.host.upper(), port=None).render_as_string(hide_password=False)

    ensure_roles(admin_url, app_url, prov_url)

    _connect(prov_url).close()


def test_refuses_to_grant_createrole_before_postgres_16(cluster, monkeypatch):
    """Before 16, CREATEROLE could alter (reset the password of) and take membership of any role
    but a superuser: the app's, and on RDS the master user. The server here is newer, so it
    reports 15 for this test."""
    admin_url, app_url, prov_url = cluster
    monkeypatch.setattr(psycopg.ConnectionInfo, "server_version", property(lambda _: 150010))

    with pytest.raises(RuntimeError, match="PostgreSQL 16"):
        ensure_roles(admin_url, app_url, prov_url)

    assert _attrs(admin_url, make_url(app_url).username) is None
    assert _attrs(admin_url, make_url(prov_url).username) is None


def test_a_master_user_whose_name_needs_quoting(cluster, admin_url):
    """RDS allows a master username like `AiosAdmin`, which isn't the same unquoted."""
    _, app_url, prov_url = cluster
    tag = uuid.uuid4().hex[:8]
    master, dbname = f"T_Master_{tag}", f"t_roles_q_{tag}"
    with _connect(admin_url) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD 'pw'").format(
                sql.Identifier(master)
            )
        )
    master_url = make_url(admin_url).set(username=master, password="pw", database=dbname)
    master_url = master_url.render_as_string(hide_password=False)
    try:
        with _connect(master_url, database="postgres") as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        with _connect(admin_url, database=dbname) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        with _connect(master_url) as conn:
            conn.execute("CREATE TABLE things (id int)")
        app_q = make_url(app_url).set(database=dbname).render_as_string(hide_password=False)
        prov_q = make_url(prov_url).set(database=dbname).render_as_string(hide_password=False)

        ensure_roles(master_url, app_q, prov_q)

        with _connect(app_q) as conn:
            conn.execute("ALTER TABLE things ADD COLUMN name text")
    finally:
        # As its owner: a master user that isn't a superuser couldn't drop it.
        with _connect(master_url, database="postgres") as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            # It created them, so only it may drop them, if it isn't a superuser.
            for role in (make_url(app_url).username, make_url(prov_url).username):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        with _connect(admin_url) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(master)))


def test_less_common_objects_are_handed_over_too(cluster):
    """As a migration might create them, and a later one alter them (dropping them needs only
    the schema, which the app's role owns; altering them needs the object)."""
    admin_url, app_url, prov_url = cluster
    with _connect(admin_url) as conn:
        conn.execute("CREATE TABLE pairs (a int, b int)")
        conn.execute("CREATE STATISTICS pairs_ab ON a, b FROM pairs")
        conn.execute(
            "CREATE COLLATION folded (provider = icu, locale = 'und', deterministic = false)"
        )
        conn.execute("CREATE TEXT SEARCH DICTIONARY plain_dict (TEMPLATE = simple)")
        conn.execute("CREATE TEXT SEARCH CONFIGURATION plain (COPY = simple)")
        conn.execute("CREATE FUNCTION near(int, int) RETURNS bool LANGUAGE sql AS 'SELECT $1 = $2'")
        conn.execute("CREATE OPERATOR ~= (LEFTARG = int, RIGHTARG = int, FUNCTION = near)")

    ensure_roles(admin_url, app_url, prov_url)

    # COMMENT ON, like ALTER, takes owning the object.
    with _connect(app_url) as conn:
        for obj in [
            "STATISTICS pairs_ab",
            "OPERATOR ~= (int, int)",
            "FUNCTION near(int, int)",
            "COLLATION folded",
            "TEXT SEARCH CONFIGURATION plain",
            "TEXT SEARCH DICTIONARY plain_dict",
        ]:
            conn.execute(sql.SQL("COMMENT ON {} IS 'migrated'").format(sql.SQL(obj)))


def test_partitioned_tables_and_their_partitions_are_handed_over(cluster):
    """A partitioned table's partitions don't follow it, as its column sequences do."""
    admin_url, app_url, prov_url = cluster
    with _connect(admin_url) as conn:
        conn.execute(
            "CREATE TABLE events (id int GENERATED ALWAYS AS IDENTITY, n serial, at date)"
            " PARTITION BY RANGE (at)"
        )
        conn.execute(
            "CREATE TABLE events_2026 PARTITION OF events"
            " FOR VALUES FROM ('2026-01-01') TO ('2027-01-01') PARTITION BY LIST (n)"
        )
        conn.execute("CREATE TABLE events_2026_rest PARTITION OF events_2026 DEFAULT")

    ensure_roles(admin_url, app_url, prov_url)

    with _connect(app_url) as conn:
        for table in ["events", "events_2026", "events_2026_rest"]:
            conn.execute(sql.SQL("COMMENT ON TABLE {} IS 'migrated'").format(sql.Identifier(table)))
        for seq in ["events_id_seq", "events_n_seq"]:
            conn.execute(sql.SQL("ALTER SEQUENCE {} RESTART").format(sql.Identifier(seq)))


def test_fails_naming_what_it_cannot_hand_over(cluster):
    """Rather than leave the app's role unable to migrate something, silently."""
    admin_url, app_url, prov_url = cluster
    with _connect(admin_url) as conn:
        conn.execute("CREATE PUBLICATION everything_pub")

    with pytest.raises(RuntimeError, match="everything_pub"):
        ensure_roles(admin_url, app_url, prov_url)

    assert _attrs(admin_url, make_url(app_url).username) is None


def _migrate(database_url: str) -> subprocess.CompletedProcess:
    """`alembic upgrade head` against `database_url`, in a process of its own (the app's settings
    are read once per process)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _setup_checkpointer(database_url: str, monkeypatch) -> None:
    """A turn's checkpointer, as `database_url`'s user: sets up its schema the first time."""
    monkeypatch.setattr(checkpoints, "_ready", False)
    monkeypatch.setattr(get_settings(), "database_url", database_url)

    async def turn() -> None:
        async with checkpoints.turn(run_id=0):
            pass

    asyncio.run(turn())


def _table_owners(url: str) -> list[tuple[str, str]]:
    """(schema, owner) for every owner of a table in the app's and the checkpointer's schemas."""
    with _connect(url) as conn:
        return conn.execute(
            "SELECT DISTINCT schemaname::text, tableowner::text FROM pg_tables"
            " WHERE schemaname IN ('public', 'langgraph') ORDER BY 1, 2"
        ).fetchall()


def _owned_by_app(app_url: str) -> list[tuple[str, str]]:
    app = make_url(app_url).username
    return [("langgraph", app), ("public", app)]


def test_migrations_and_checkpointer_setup_run_as_app_role(cluster, monkeypatch):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)

    result = _migrate(app_url)
    assert result.returncode == 0, result.stderr
    _setup_checkpointer(app_url, monkeypatch)

    assert _table_owners(app_url) == _owned_by_app(app_url)


# Objects in the database's own schemas, other than extensions' (pgvector's stay the master
# user's), whose owner is `%s`. Looked up by name in pg_roles, not cast with ::regrole, which
# reads it as an unquoted identifier (so a master user like `AiosAdmin` isn't found).
_OWNED_BY = """
SELECT kind, name FROM (
    SELECT 'schema' AS kind, nspname::text AS name, nspowner AS owner,
           'pg_namespace'::regclass AS cls, oid
      FROM pg_namespace
     WHERE nspname NOT LIKE 'pg\\_%%' AND nspname <> 'information_schema'
    UNION ALL
    SELECT 'relation', c.oid::regclass::text, c.relowner, 'pg_class'::regclass, c.oid
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
    UNION ALL
    SELECT 'type', t.oid::regtype::text, t.typowner, 'pg_type'::regclass, t.oid
      FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text, p.proowner, 'pg_proc'::regclass, p.oid
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
) o
WHERE owner = (SELECT oid FROM pg_roles WHERE rolname = %s)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d
                   WHERE d.classid = o.cls AND d.objid = o.oid AND d.deptype = 'e')
ORDER BY 1, 2
"""


def test_a_database_migrated_as_the_master_user_is_handed_to_app_role(cluster, monkeypatch):
    admin_url, app_url, prov_url = cluster
    admin = make_url(admin_url).username
    # As deployed before this role existed: migrated and checkpointed as the master user, with
    # a view and a function of its own besides.
    assert _migrate(admin_url).returncode == 0
    _setup_checkpointer(admin_url, monkeypatch)
    with _connect(admin_url) as conn:
        conn.execute("CREATE VIEW recent_users AS SELECT id FROM users")
        conn.execute("CREATE FUNCTION one() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        assert conn.execute(_OWNED_BY, (admin,)).fetchall()

    ensure_roles(admin_url, app_url, prov_url)

    with _connect(admin_url) as conn:
        assert conn.execute(_OWNED_BY, (admin,)).fetchall() == []
    # The app's role can change what it now owns: drop the master user's view and function, then
    # migrate all the way down (to `base`, not a revision that a squash would rename) and up
    # again, and the checkpointer's setup.
    with _connect(app_url) as conn:
        conn.execute("DROP VIEW recent_users")
        conn.execute("DROP FUNCTION one()")
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": app_url},
        capture_output=True,
        text=True,
    )
    assert down.returncode == 0, down.stderr
    up = _migrate(app_url)
    assert up.returncode == 0, up.stderr
    _setup_checkpointer(app_url, monkeypatch)
    assert _table_owners(app_url) == _owned_by_app(app_url)
