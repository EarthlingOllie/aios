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
from aios.db_roles import ensure_roles

BACKEND = Path(__file__).resolve().parents[1]


def _libpq(url: str, **parts) -> str:
    return make_url(url).set(drivername="postgresql", **parts).render_as_string(hide_password=False)


def _connect(url: str, **parts) -> psycopg.Connection:
    return psycopg.connect(_libpq(url, **parts), autocommit=True)


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


def test_rerunning_changes_the_passwords(cluster):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)
    rotated = make_url(app_url).set(password="rotated").render_as_string(hide_password=False)

    ensure_roles(admin_url, rotated, prov_url)

    _connect(rotated).close()
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        _connect(app_url)


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
    monkeypatch.setattr(checkpoints, "_ready", False)

    async def setup() -> None:
        conn = await psycopg.AsyncConnection.connect(_libpq(database_url), autocommit=True)
        async with conn:
            await checkpoints.setup(conn)

    asyncio.run(setup())


def _table_owners(url: str) -> list[tuple[str]]:
    with _connect(url) as conn:
        return conn.execute(
            "SELECT DISTINCT tableowner FROM pg_tables"
            " WHERE schemaname IN ('public', 'langgraph') ORDER BY 1"
        ).fetchall()


def test_migrations_and_checkpointer_setup_run_as_app_role(cluster, monkeypatch):
    admin_url, app_url, prov_url = cluster
    ensure_roles(admin_url, app_url, prov_url)

    result = _migrate(app_url)
    assert result.returncode == 0, result.stderr
    _setup_checkpointer(app_url, monkeypatch)

    assert _table_owners(app_url) == [(make_url(app_url).username,)]


# Objects in the database's own schemas, other than extensions' (pgvector's stay the master
# user's), whose owner is `%s`.
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
WHERE owner = %s::regrole
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
    # The app's role can change what it now owns: migrate down and up again, and the
    # checkpointer's setup.
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0004"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": app_url},
        capture_output=True,
        text=True,
    )
    assert down.returncode == 0, down.stderr
    up = _migrate(app_url)
    assert up.returncode == 0, up.stderr
    _setup_checkpointer(app_url, monkeypatch)
    assert _table_owners(app_url) == [(make_url(app_url).username,)]
