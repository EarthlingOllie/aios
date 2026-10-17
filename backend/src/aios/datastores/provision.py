"""Creating and migrating data stores, as the provisioner (DATA_STORE_PROVISIONER_URL).

`provision` gives a store its database, `ds_<id as hex>`, on the provisioner's instance, with
three roles named after it (never after a user):

- `<prefix>_owner` (no login) owns the database and everything in it. The provisioner is a
  member, and `SET ROLE`s to it to migrate the store.
- `<prefix>_rw` and `<prefix>_ro` (login, with the store's encrypted passwords) may connect to
  it and nothing else, use its schemas, and read and write (`rw`) or only read (`ro`) its
  tables, but for the tree's bookkeeping (`aios.alembic_version`, `aios.schema_history`),
  which both only read. Neither may create anything. Each has its own connection limit, and
  default statement and idle-in-transaction timeouts, which a session may change
  (`data_store_*` settings).

Nobody else may connect (`CONNECT` is revoked from `PUBLIC`; until it is, the database takes no
connections at all), and the database has no `public` schema. It's copied from the store
template (`aios.db_roles`), which brings pgvector in an `extensions` schema: the provisioner
isn't a superuser, so it couldn't install it.

Then it runs the store tree to its head, as the owner (`upgrade_store`), and marks the store
`ready`. Every step checks what's already there, so after a crash running it again finishes
the job; runs of the same store wait for each other.

`migrate_all` brings every ready store to the tree's head, one at a time: a store that fails is
reported and the rest carry on. It also finishes provisioning stores that never got there.
`python -m aios.datastores.provision migrate-all` runs it (the deploy does, after the
platform's migrations). Between deploys, `retry_unfinished` tries those stores again every few
minutes, so a store whose provisioning gave up (the instance was down, say) gets there once it
can.
"""

import logging
import sys
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from celery.exceptions import SoftTimeLimitExceeded
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

# Seconds the provisioner waits to connect before giving up on an instance: the one provisioning
# worker shouldn't sit on one that never answers while new stores queue behind it.
CONNECT_TIMEOUT = 10


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


def _create_roles(run: _Run) -> None:
    conn, n, settings = run.server, run.names, get_settings()
    for role, password in ((n.owner, None), (n.rw, run.rw_password), (n.ro, run.ro_password)):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        if password is None:
            continue
        # The verifier, not the password, so no statement log ever holds it.
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN CONNECTION LIMIT {} PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(settings.data_store_connection_limit),
                sql.Literal(credentials.scram_verifier(password)),
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
    # Closed: a new database lets every role connect, and one let in before `_restrict_connect`
    # would stay connected.
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {} ALLOW_CONNECTIONS false").format(
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
    conn.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(db))


@contextmanager
def _as_owner(names: StoreNames) -> Iterator[psycopg.Connection]:
    """The provisioner on the store's database, acting as its owner."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(
        libpq_conninfo(url, database=names.dbname),
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT,
    ) as conn:
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


# The platform's record of the store's schema, which the default privileges would otherwise let
# `_rw` rewrite: only the tree, as the owner, changes them.
BOOKKEEPING = ("aios.alembic_version", "aios.schema_history")


def upgrade_store(names: StoreNames) -> str:
    """Run the store tree to its head on the store's database, as its owner, and keep `_rw` off
    its bookkeeping (`BOOKKEEPING`); returns the revision it's at."""
    url = make_url(get_settings().data_store_provisioner_url).set(database=names.dbname)
    engine = create_engine(
        url, poolclass=pool.NullPool, connect_args={"connect_timeout": CONNECT_TIMEOUT}
    )
    try:
        with engine.connect() as conn:
            quote = conn.dialect.identifier_preparer.quote
            conn.exec_driver_sql(f"SET ROLE {quote(names.owner)}")
            conn.commit()
            command.upgrade(migrations.config(conn), "head")
            # Every time, not in a revision: the tree doesn't know the store's role names.
            conn.exec_driver_sql(
                f"REVOKE INSERT, UPDATE, DELETE ON {', '.join(BOOKKEEPING)} FROM {quote(names.rw)}"
            )
            revision = conn.scalar(text("SELECT version_num FROM aios.alembic_version"))
            conn.commit()
            return revision
    finally:
        engine.dispose()


def _migrate(run: _Run) -> None:
    run.revision = upgrade_store(run.names)


def _mark_ready(run: _Run) -> None:
    # No host or port: it's on the platform database's instance, which the app reaches at its
    # own address (`connections.store_url`), not necessarily the provisioner's.
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == run.store_id)
            .values(
                status=DataStoreStatus.ready,
                schema_revision=run.revision,
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
def _provisioner_locked(
    store_id: uuid.UUID, wait: bool = True
) -> Iterator[psycopg.Connection | None]:
    """The provisioner on the platform database, holding the store's lock (released when the
    connection closes, however the run ends); unless `wait`, None if another run holds it."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(
        libpq_conninfo(url), autocommit=True, connect_timeout=CONNECT_TIMEOUT
    ) as conn:
        key = (f"aios.data_store:{store_id}",)
        if wait:
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", key)
        else:
            got = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", key)
            if not got.fetchone()[0]:
                yield None
                return
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
        # Concurrent runs wait for each other, but the row could be changed by others. Reloaded
        # (populate_existing): the session would otherwise keep what it read before the lock.
        store = db.scalar(
            select(DataStore)
            .where(DataStore.id == store_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if store is None:
            return None
        if store.rw_password is None:
            store.rw_password = credentials.encrypt(credentials.new_password())
        if store.ro_password is None:
            store.ro_password = credentials.encrypt(credentials.new_password())
        db.commit()
        return credentials.decrypt(store.rw_password), credentials.decrypt(store.ro_password)


def provision(store_id: uuid.UUID, wait: bool = True) -> str | None:
    """Create the store's database and roles, migrate it and mark it ready (see the module
    docstring); returns the revision it's at. Does nothing (returns None) for a store that's
    gone, or that's past provisioning, or, unless `wait`, that another run is on."""
    with _provisioner_locked(store_id, wait) as server:
        if server is None:
            return None
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
    """Bring a ready store to the tree's head and record its revision."""
    with _provisioner_locked(store_id):
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status != DataStoreStatus.ready:
                raise LookupError(f"Data store {store_id} isn't ready")
            names = StoreNames.of(store)
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


def _finish(which: ColumnElement[bool], wait: bool = True) -> dict[uuid.UUID, str | BaseException]:
    """Provision the stores `which` picks, one at a time; returns the revision each reached, or
    what stopped it (and marks it failed), without stopping the rest. Unless `wait`, skips a
    store another run is on."""
    outcome: dict[uuid.UUID, str | BaseException] = {}
    with SessionLocal() as db:
        unfinished = db.scalars(select(DataStore.id).where(which)).all()
    for store_id in unfinished:
        try:
            revision = provision(store_id, wait)
            if revision is not None:
                outcome[store_id] = revision
        except SoftTimeLimitExceeded:
            # The provisioning worker's time limit, for the whole run: not this store's failure.
            raise
        except Exception as exc:  # noqa: BLE001 - one store's failure is reported, not raised
            log.exception("Provisioning data store %s failed", store_id)
            fail(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


# A store still provisioning this long after it was made (which takes seconds) is tried again
# here, rather than left to its task: that may never have been queued, or may be stuck with a
# worker that was killed (Redis redelivers it only after its visibility timeout, over an hour).
# If its task is only waiting to try again, this is safe: runs of a store wait for each other,
# and one that's ready is left alone. (A failure here marks it failed while its retries go on.)
UNFINISHED_AFTER = timedelta(minutes=5)


def retry_unfinished(
    store_ids: Collection[uuid.UUID] | None = None,
) -> dict[uuid.UUID, str | BaseException]:
    """Try once more to provision the stores whose provisioning gave up, or hasn't finished
    (`UNFINISHED_AFTER`); the provisioning worker runs it every few minutes
    (`aios.worker.RETRY_UNFINISHED_EVERY`). Returns the revision each reached, or what stopped
    it. A store another run is on (a deploy's migrate-all, say) is left to it: new stores'
    provisioning waits behind this, on the one provisioning worker."""
    return _finish(
        and_(
            _only(store_ids),
            or_(
                DataStore.status == DataStoreStatus.failed,
                and_(
                    DataStore.status == DataStoreStatus.provisioning,
                    DataStore.created_at < func.now() - UNFINISHED_AFTER,
                ),
            ),
        ),
        wait=False,
    )


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
    with SessionLocal() as db:
        ready = db.scalars(
            select(DataStore.id)
            .where(only, DataStore.status == DataStoreStatus.ready)
            .order_by(DataStore.created_at)
        ).all()
    for store_id in ready:
        if store_id in outcome:  # just provisioned, so already at head
            continue
        try:
            outcome[store_id] = migrate_store(store_id)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Migrating data store %s failed", store_id)
            _record_error(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


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
