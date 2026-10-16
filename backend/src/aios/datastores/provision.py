"""Creating and migrating data stores, as the provisioner (DATA_STORE_PROVISIONER_URL).

`provision` gives a store its database, `ds_<id as hex>`, on the provisioner's instance, with
three roles named after it (never after a user):

- `<prefix>_owner` (no login) owns the database and everything in it. The provisioner is a
  member, and `SET ROLE`s to it to migrate the store.
- `<prefix>_rw` and `<prefix>_ro` (login, with the store's encrypted passwords) may connect to
  it and nothing else, use its schemas, and read and write (`rw`) or only read (`ro`) its
  tables. Neither may create anything. Each has its own statement timeout, idle-in-transaction
  timeout and connection limit (`data_store_*` settings).

Nobody else may connect (`CONNECT` is revoked from `PUBLIC`), and the database has no `public`
schema. It's copied from the store template (`aios.db_roles`), which brings pgvector in an
`extensions` schema: the provisioner isn't a superuser, so it couldn't install it.

Then it runs the store tree to its head, as the owner (`upgrade_store`), and marks the store
`ready`. Every step checks what's already there, so after a crash running it again finishes
the job; runs of the same store wait for each other.

`migrate_all` brings every ready store to the tree's head, and its roles to the limits
configured now, one at a time: a store that fails is reported and the rest carry on. It also
finishes provisioning stores that never got there. `python -m aios.datastores.provision
migrate-all` runs it (the deploy does, after the platform's migrations). Between deploys,
`retry_unfinished` tries those stores again every few minutes, so a store whose provisioning
gave up (the instance was down, say) gets there once it can. It also brings ready stores behind
the tree's head to it: the release before a deploy can still make one after the deploy migrated
stores, and migrating one can fail.
"""

import logging
import sys
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from psycopg import sql
from sqlalchemy import (
    ColumnElement,
    and_,
    create_engine,
    func,
    or_,
    pool,
    select,
    text,
    true,
    update,
)
from sqlalchemy.engine import make_url

from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, migrations
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus
from alembic import command

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoreNames:
    dbname: str
    owner: str
    rw: str
    ro: str

    @classmethod
    def of(cls, store: DataStore) -> "StoreNames":
        p = store.role_prefix
        return cls(store.dbname, f"{p}_owner", f"{p}_rw", f"{p}_ro")


def names_for(store_id: uuid.UUID) -> tuple[str, str]:
    """A new store's database name and role prefix: from its id, never its owner's."""
    name = f"ds_{store_id.hex}"
    return name, name


@dataclass
class _Run:
    """What the steps of one provisioning run share."""

    store_id: uuid.UUID
    names: StoreNames
    rw_password: str
    ro_password: str
    server: psycopg.Connection  # the provisioner, on the platform database
    revision: str | None = None


def _ident(*names: str) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(n) for n in names)


def _set_limits(conn: psycopg.Connection, names: StoreNames) -> None:
    """Give the store's login roles the limits configured now (`data_store_*` settings): when
    they're made, and on every migration, so a changed setting reaches existing stores."""
    settings = get_settings()
    for role in (names.rw, names.ro):
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH CONNECTION LIMIT {}").format(
                sql.Identifier(role), sql.Literal(settings.data_store_connection_limit)
            )
        )
        for setting, value in (
            ("statement_timeout", settings.data_store_statement_timeout),
            (
                "idle_in_transaction_session_timeout",
                settings.data_store_idle_in_transaction_timeout,
            ),
        ):
            conn.execute(
                sql.SQL("ALTER ROLE {} SET {} = {}").format(
                    sql.Identifier(role), sql.Identifier(setting), sql.Literal(value)
                )
            )


def _create_roles(run: _Run) -> None:
    conn, n = run.server, run.names
    for role, password in ((n.owner, None), (n.rw, run.rw_password), (n.ro, run.ro_password)):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        if password is None:
            continue
        # The verifier, not the password, so no statement log ever holds it.
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(credentials.scram_verifier(password))
            )
        )
    _set_limits(conn, n)
    # To create the database owned by it, and to act as it inside.
    conn.execute(
        sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET TRUE").format(
            sql.Identifier(n.owner)
        )
    )


def _create_database(run: _Run) -> None:
    conn, n = run.server, run.names
    if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (n.dbname,)).fetchone():
        return
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
            sql.Identifier(n.dbname),
            sql.Identifier(n.owner),
            sql.Identifier(get_settings().data_store_template),
        )
    )


def _restrict_connect(run: _Run) -> None:
    conn, n = run.server, run.names
    db = sql.Identifier(n.dbname)
    # ALL: CONNECT, and TEMPORARY, which would let any role create temporary tables.
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, _ident(n.rw, n.ro)))


@contextmanager
def _as_owner(names: StoreNames) -> Iterator[psycopg.Connection]:
    """The provisioner on the store's database, acting as its owner."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url, database=names.dbname), autocommit=True) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(names.owner)))
        yield conn


def _set_up_database(run: _Run) -> None:
    n = run.names
    with _as_owner(n) as conn:
        # Already there from the template; a no-op unless the template lacks it.
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions")
        conn.execute("DROP SCHEMA IF EXISTS public")
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET search_path = aios, extensions").format(
                sql.Identifier(n.dbname)
            )
        )
        owner, both = sql.Identifier(n.owner), _ident(n.rw, n.ro)
        for grant in (
            sql.SQL("GRANT USAGE ON SCHEMAS TO {}").format(both),
            sql.SQL("GRANT SELECT ON TABLES TO {}").format(both),
            sql.SQL("GRANT INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(n.rw)),
            sql.SQL("GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(n.rw)),
        ):
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} ").format(owner) + grant)


def upgrade_store(names: StoreNames) -> str:
    """Run the store tree to its head on the store's database, as its owner; returns the
    revision it's at."""
    url = make_url(get_settings().data_store_provisioner_url).set(database=names.dbname)
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f"SET ROLE {conn.dialect.identifier_preparer.quote(names.owner)}")
            conn.commit()
            command.upgrade(migrations.config(conn), "head")
            revision = conn.scalar(text("SELECT version_num FROM aios.alembic_version"))
            conn.commit()
            return revision
    finally:
        engine.dispose()


def _migrate(run: _Run) -> None:
    run.revision = upgrade_store(run.names)


def _mark_ready(run: _Run) -> None:
    prov = make_url(get_settings().data_store_provisioner_url)
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == run.store_id)
            .values(
                status=DataStoreStatus.ready,
                schema_revision=run.revision,
                host=prov.host,
                port=prov.port or 5432,
                error=None,
            )
        )
        db.commit()


# In order. Each can be run again, whatever the ones before it left.
STEPS: list[Callable[[_Run], None]] = [
    _create_roles,
    _create_database,
    _restrict_connect,
    _set_up_database,
    _migrate,
    _mark_ready,
]


@contextmanager
def _provisioner_locked(store_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """The provisioner on the platform database, holding the store's lock (released when the
    connection closes, however the run ends)."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"aios.data_store:{store_id}",)
        )
        yield conn


def _passwords(store_id: uuid.UUID) -> tuple[str, str] | None:
    """The store's login roles' passwords, generating any it lacks (stores made before they
    were generated with the row); None if the store is gone."""
    with SessionLocal() as db:
        store = db.get(DataStore, store_id)
        if store is None:
            return None
        if store.rw_password is not None and store.ro_password is not None:
            return credentials.decrypt(store.rw_password), credentials.decrypt(store.ro_password)
        # Concurrent runs wait for each other, but the row could be changed by others.
        store = db.scalar(select(DataStore).where(DataStore.id == store_id).with_for_update())
        if store is None:
            return None
        if store.rw_password is None:
            store.rw_password = credentials.encrypt(credentials.new_password())
        if store.ro_password is None:
            store.ro_password = credentials.encrypt(credentials.new_password())
        db.commit()
        return credentials.decrypt(store.rw_password), credentials.decrypt(store.ro_password)


def provision(store_id: uuid.UUID) -> str | None:
    """Create the store's database and roles, migrate it and mark it ready (see the module
    docstring); returns the revision it's at. Does nothing (returns None) for a store that's
    gone, or that's past provisioning."""
    with _provisioner_locked(store_id) as server:
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status not in (
                DataStoreStatus.provisioning,
                DataStoreStatus.failed,
            ):
                return None
            names = StoreNames.of(store)
        passwords = _passwords(store_id)
        if passwords is None:
            return None
        run = _Run(store_id, names, *passwords, server=server)
        for step in STEPS:
            step(run)
    log.info("Data store %s is ready at revision %s", store_id, run.revision)
    return run.revision


def drop(names: StoreNames) -> None:
    """Drop a store's database and roles, if they exist. Only tests use it for now: deleting
    users' stores (after a retention period) is to come."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (names.dbname,)).fetchone():
            db = sql.Identifier(names.dbname)
            conn.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(db))
            # End its sessions. Ending the login roles' takes their privileges: the provisioner
            # only has ADMIN on them, from creating them.
            for role in (names.rw, names.ro):
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    conn.execute(
                        sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET FALSE").format(
                            sql.Identifier(role)
                        )
                    )
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pg_has_role(usesysid, 'USAGE')",
                (names.dbname,),
            )
            # Not WITH (FORCE), which refuses while an autovacuum worker is in the database (it
            # has no role, so only a superuser may end it). A plain drop stops the worker
            # itself, and waits a few seconds for the sessions ended above to exit.
            conn.execute(sql.SQL("DROP DATABASE {}").format(db))
        for role in (names.rw, names.ro, names.owner):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def fail(store_id: uuid.UUID, error: str) -> None:
    """Mark a store's provisioning failed (it gave up retrying), with why."""
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(
                DataStore.id == store_id,
                DataStore.status.in_([DataStoreStatus.provisioning, DataStoreStatus.failed]),
            )
            .values(status=DataStoreStatus.failed, error=error)
        )
        db.commit()


def migrate_store(store_id: uuid.UUID) -> str:
    """Bring a ready store to the tree's head, and its roles to the limits configured now, and
    record its revision."""
    with _provisioner_locked(store_id) as server:
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status != DataStoreStatus.ready:
                raise LookupError(f"Data store {store_id} isn't ready")
            names = StoreNames.of(store)
        _set_limits(server, names)
        revision = upgrade_store(names)
        with SessionLocal() as db:
            db.execute(
                update(DataStore)
                .where(DataStore.id == store_id)
                .values(schema_revision=revision, error=None)
            )
            db.commit()
        return revision


def _record_error(store_id: uuid.UUID, error: str) -> None:
    with SessionLocal() as db:
        db.execute(update(DataStore).where(DataStore.id == store_id).values(error=error))
        db.commit()


def describe_error(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:2000]


def _only(store_ids: Collection[uuid.UUID] | None) -> ColumnElement[bool]:
    return DataStore.id.in_(store_ids) if store_ids is not None else true()


def _finish(which: ColumnElement[bool]) -> dict[uuid.UUID, str | BaseException]:
    """Provision the stores `which` picks, one at a time; returns the revision each reached, or
    what stopped it (and marks it failed), without stopping the rest."""
    outcome: dict[uuid.UUID, str | BaseException] = {}
    with SessionLocal() as db:
        unfinished = db.scalars(select(DataStore.id).where(which)).all()
    for store_id in unfinished:
        try:
            revision = provision(store_id)
            if revision is not None:
                outcome[store_id] = revision
        except Exception as exc:  # noqa: BLE001 - one store's failure is reported, not raised
            log.exception("Provisioning data store %s failed", store_id)
            fail(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


# A store still provisioning this long after it was made isn't being retried any more: its task
# was never queued, or was lost. (Its own retries give up after about half an hour.)
UNFINISHED_AFTER = timedelta(hours=1)


def _migrate_ready(
    which: ColumnElement[bool], skip: Collection[uuid.UUID] = ()
) -> dict[uuid.UUID, str | BaseException]:
    """Bring the ready stores `which` picks (but `skip`) to the tree's head, one at a time;
    returns the revision each reached, or what stopped it (recorded on the store)."""
    outcome: dict[uuid.UUID, str | BaseException] = {}
    with SessionLocal() as db:
        ready = db.scalars(
            select(DataStore.id)
            .where(which, DataStore.status == DataStoreStatus.ready)
            .order_by(DataStore.created_at)
        ).all()
    for store_id in ready:
        if store_id in skip:
            continue
        try:
            outcome[store_id] = migrate_store(store_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Migrating data store %s failed", store_id)
            _record_error(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


def _behind_head() -> ColumnElement[bool]:
    """Stores at an earlier revision of the tree than its head. Not one it doesn't know: the
    release after this one has already migrated that store."""
    return DataStore.schema_revision.in_(migrations.revisions_before_head())


def retry_unfinished(
    store_ids: Collection[uuid.UUID] | None = None,
) -> dict[uuid.UUID, str | BaseException]:
    """Try once more to provision the stores whose provisioning gave up, or never ran
    (`UNFINISHED_AFTER`), and bring ready stores behind the tree's head to it (the release
    before this one made them after the deploy migrated stores, or migrating them failed); the
    provisioning worker runs it every few minutes (`aios.worker.RETRY_UNFINISHED_EVERY`).
    Returns the revision each reached, or what stopped it."""
    only = _only(store_ids)
    outcome = _finish(
        and_(
            only,
            or_(
                DataStore.status == DataStoreStatus.failed,
                and_(
                    DataStore.status == DataStoreStatus.provisioning,
                    DataStore.created_at < func.now() - UNFINISHED_AFTER,
                ),
            ),
        )
    )
    return outcome | _migrate_ready(and_(only, _behind_head()), skip=outcome)


def migrate_all(
    store_ids: Collection[uuid.UUID] | None = None,
) -> dict[uuid.UUID, str | BaseException]:
    """Finish provisioning stores that never got there, then bring every ready store to the
    tree's head, one at a time. Returns each store's revision, or what stopped it: a store that
    fails doesn't stop the rest.

    `store_ids` limits it to those stores (tests use it to leave other stores alone)."""
    only = _only(store_ids)
    outcome = _finish(
        and_(only, DataStore.status.in_([DataStoreStatus.provisioning, DataStoreStatus.failed]))
    )
    # Those just provisioned are already at head.
    return outcome | _migrate_ready(only, skip=outcome)


def main(argv: list[str]) -> int:
    if argv != ["migrate-all"]:
        print("usage: python -m aios.datastores.provision migrate-all", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)
    outcome = migrate_all()
    failed = {k: v for k, v in outcome.items() if isinstance(v, BaseException)}
    for store_id, result in outcome.items():
        print(f"{store_id}: {describe_error(result) if store_id in failed else result}")
    print(f"{len(outcome) - len(failed)} data stores at head, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
