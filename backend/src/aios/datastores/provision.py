"""Creating and migrating data stores, as the provisioner (DATA_STORE_PROVISIONER_URL).

`provision` gives a store its database, `ds_<id as hex>`, on the provisioner's instance, with
three roles named after it (never after a user):

- `<prefix>_owner` (no login) owns the database and everything in it. The provisioner is a
  member, and `SET ROLE`s to it to migrate the store.
- `<prefix>_rw` and `<prefix>_ro` (login, with the store's encrypted passwords) may connect to
  it and nothing else, use its schemas, and read and write (`rw`) or only read (`ro`) its
  tables, but for the tree's bookkeeping (`aios.alembic_version`, `aios.schema_history`,
  `aios.embedding_columns`, `aios.dropped_objects`), which both only read, and dropped tables
  and columns (`aios.datastores.dropped`), which neither may. Neither may create anything.
  Each has its own connection limit, and default statement and idle-in-transaction timeouts,
  which a session may change (`data_store_*` settings).
- `<prefix>_writer` (no login), which `write_data` writes as: `rw` may `SET ROLE` to it but
  doesn't inherit it, and it may write in the `data` schema and nowhere else (`_grant_writer`).
- The deny list (`DENIED_COLUMNS`): columns neither `ro` nor the writer may read (people's
  emails in the people mirror, the audit log's details), applied each time the store is
  migrated. The audit log is append-only to `rw` too (`APPEND_ONLY`), and the `reporting`
  views read-only.

Nobody else may connect (`CONNECT` is revoked from `PUBLIC`; until it is, the database takes no
connections at all), and the database has no `public` schema. It's copied from the store
template (`aios.db_roles`), which brings pgvector and pgstattuple (for measuring the store,
`aios.datastores.quota`) in an `extensions` schema: the provisioner isn't a superuser, so it
couldn't install them.

Then it runs the store tree to its head, as the owner (`upgrade_store`), and marks the store
`ready`. The tree's `history` line too, if the store's owner has switched the run history
export on (`aios.datastores.history`), and from then on whenever the store is migrated.

Every step checks what's already there, so after a crash running it again finishes the job;
runs of the same store wait for each other.

`migrate_all` brings every ready store to the tree's head, and its roles to the limits
configured now, one at a time: a store that fails is reported and the rest carry on. It also
finishes provisioning stores that never got there. `python -m aios.datastores.provision
migrate-all` runs it (the deploy does, after the platform's migrations). Between deploys,
`retry_unfinished` tries those stores again every few minutes, so a store whose provisioning
gave up (the instance was down, say) gets there once it can. It also brings ready stores behind
the tree's head to it: the release before a deploy can still make one after the deploy migrated
stores, and migrating one can fail.

**Deleting.** A store goes `deleting`, with no organisation, when its organisation is deleted.
The provisioning
worker's sweep (`delete_deleting`, queued then and run on its schedule too) disposes of this
process's engines on it, drops its database and roles, deletes its files under its prefix and
marks it `deleted`. As provisioning's, each step can be run again whatever the ones before left,
so a sweep cut short is finished by the next. It waits for a provisioning run still on the store
(they share its lock), which runs its steps out but doesn't mark it ready: the store went
`deleting` under it (its organisation was deleted straight after it was created).
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
    delete,
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
from aios.datastores import connections, credentials, files, migrations
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus, HistoryExport
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

    @property
    def writer(self) -> str:
        """The role `write_data` writes as (`WRITER_SUFFIX`)."""
        return writer_of(self.rw)


# `write_data`'s role: no login, and `rw` may only `SET ROLE` to it (it doesn't inherit its
# privileges, nor they rw's), so the tool writes nowhere but the `data` schema.
WRITER_SUFFIX = "_writer"


def writer_of(rw: str) -> str:
    """The name of the writer role of the store whose read-write role is `rw`."""
    return rw.removesuffix("_rw") + WRITER_SUFFIX


# The statuses a provisioning run takes a store in, and marks it ready from.
PROVISIONABLE = (DataStoreStatus.provisioning, DataStoreStatus.failed)


@dataclass
class _Run:
    """What the steps of one provisioning run share."""

    store_id: uuid.UUID
    names: StoreNames
    rw_password: str
    ro_password: str
    server: psycopg.Connection  # the provisioner, on the platform database
    history: bool = False  # bring the store's `history` line to its head too
    revisions: "Revisions | None" = None
    ready: bool = False  # `_mark_ready` marked it


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
    _create_writer(conn, n)
    # To create the database owned by it, and to act as it inside.
    conn.execute(
        sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET TRUE").format(
            sql.Identifier(n.owner)
        )
    )


def _create_writer(conn: psycopg.Connection, names: StoreNames) -> None:
    """The store's writer role (`WRITER_SUFFIX`), which `rw` may `SET ROLE` to but doesn't
    inherit: when the store is made, and on every migration, for stores made before it."""
    writer = sql.Identifier(names.writer)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (names.writer,)).fetchone():
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(writer))
    conn.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
            writer, sql.Identifier(names.rw)
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
def as_owner(names: StoreNames) -> Iterator[psycopg.Connection]:
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
    with as_owner(n) as conn:
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


# The platform's record of the store's schema (and which `data` columns are embeddings), which
# the default privileges would otherwise let `_rw` rewrite: only the tree and schema changes, as
# the owner, change them.
BOOKKEEPING = (
    "aios.alembic_version",
    "aios.schema_history",
    "aios.embedding_columns",
    "aios.dropped_objects",
    # The checkpointer's own record of its migrations (`aios.checkpoints.set_up_store`).
    "langgraph.checkpoint_migrations",
)


# The read-write role's search path in its store: the database's (`_set_up_database`), then the
# checkpointer's schema (`aios.checkpoints.SCHEMA`), whose tables no other schema's names clash
# with.
RW_SEARCH_PATH = ("aios", "extensions", "langgraph")


# The deny list: columns of the platform's tables in the store that neither the read-only role
# nor the writer may read (so `query_data`, `describe_data` and the Tables view never see them),
# though the read-write role, the platform's own, may. Each table's other columns stay readable.
DENIED_COLUMNS: dict[str, tuple[str, ...]] = {
    "aios.people": ("email",),
    # The bearer token a run's sandbox takes.
    "aios.sandbox_sessions": ("secret",),
    # Free-form: agents read the rest, and the reporting view (`reporting.audit_log`).
    "aios.audit_log": ("details",),
}

# What the read-write role, the platform's own, may only insert and read: the audit log is
# append-only (its trigger refuses even the owner; this is the first line).
APPEND_ONLY = ("aios.audit_log",)
# The read-only views agents read (`reporting`): the read-write role only reads them too.
REPORTING = "reporting"

# What the writer may read besides `data`: what `write_data` plans a write with.
WRITER_READS = ("aios.embedding_columns", "aios.dropped_objects")


def _apply_deny_list(conn: psycopg.Connection, names: StoreNames) -> None:
    """Take the denied columns (`DENIED_COLUMNS`) away from the read-only role and the writer,
    leaving them the rest. Every time the store is migrated: the default privileges give a new
    table's every column to both."""
    readers = _ident(names.ro, names.writer)
    for table, denied in DENIED_COLUMNS.items():
        if not conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]:
            continue
        schema, _, name = table.partition(".")
        relation = sql.Identifier(schema, name)
        columns = [
            c
            for (c,) in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s)"
                " AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                (table,),
            )
            if c not in denied
        ]
        conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(relation, readers))
        conn.execute(
            sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(
                _ident(*columns), relation, _ident(names.ro)
            )
        )


def _grant_writer(conn: psycopg.Connection, names: StoreNames) -> None:
    """Give the writer what `write_data` needs, and nothing more: to read, write and delete in
    the `data` schema (each table as `dropped.grant_access` says), and to read what it plans
    with. Every time the store is migrated, as the owner."""
    from aios.datastores import dropped

    owner, writer = sql.Identifier(names.owner), sql.Identifier(names.writer)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA aios, data TO {}").format(writer))
    for table in WRITER_READS:
        if conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]:
            schema, _, name = table.partition(".")
            conn.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier(schema, name), writer
                )
            )
    for grant in (
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(writer),
        sql.SQL("GRANT USAGE, SELECT ON SEQUENCES TO {}").format(writer),
    ):
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA data ").format(owner) + grant
        )
    conn.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA data TO {}").format(writer)
    )
    tables = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'data' AND c.relkind IN ('r', 'p')"
    ).fetchall()
    for (table,) in tables:
        dropped.grant_access(conn, names, table)


@dataclass(frozen=True)
class Revisions:
    """Where a store is on each line of the store tree (`aios.datastores.migrations`)."""

    aios: str
    # None if it hasn't got the `history` line.
    history: str | None = None


def upgrade_store(names: StoreNames, *, history: bool = False) -> Revisions:
    """Run the store tree's `aios` line to its head on the store's database, as its owner, and
    its `history` line too if `history` is set or the store already has it, and keep `_rw` off
    the tree's bookkeeping (`BOOKKEEPING`); returns the revisions it's at."""
    url = make_url(get_settings().data_store_provisioner_url).set(database=names.dbname)
    engine = create_engine(
        url, poolclass=pool.NullPool, connect_args={"connect_timeout": CONNECT_TIMEOUT}
    )
    try:
        with engine.connect() as conn:
            quote = conn.dialect.identifier_preparer.quote
            # As the provisioner, which made the role: the search path that finds the
            # checkpointer's tables (`aios.checkpoints`), the read-write role's default in the
            # store, as nothing may be set at connection startup through the pooler.
            conn.exec_driver_sql(
                f"ALTER ROLE {quote(names.rw)} IN DATABASE {quote(names.dbname)}"
                f" SET search_path = {', '.join(RW_SEARCH_PATH)}"
            )
            conn.exec_driver_sql(f"SET ROLE {quote(names.owner)}")
            conn.commit()
            cfg = migrations.config(conn)
            # By revision: the tree has a head per line, so "head" alone is ambiguous.
            command.upgrade(cfg, migrations.head_revision())
            at = conn.scalars(text("SELECT version_num FROM aios.alembic_version")).all()
            if history or any(migrations.is_history_revision(r) for r in at):
                command.upgrade(cfg, f"{migrations.HISTORY}@head")
                at = conn.scalars(text("SELECT version_num FROM aios.alembic_version")).all()
            # Every time, not in a revision: the tree doesn't know the store's role names.
            conn.exec_driver_sql(
                f"REVOKE INSERT, UPDATE, DELETE ON {', '.join(BOOKKEEPING)} FROM {quote(names.rw)}"
            )
            conn.exec_driver_sql(
                f"REVOKE UPDATE, DELETE, TRUNCATE ON {', '.join(APPEND_ONLY)}"
                f" FROM {quote(names.rw)}"
            )
            conn.exec_driver_sql(
                f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA {REPORTING}"
                f" FROM {quote(names.rw)}"
            )
            pg = conn.connection.driver_connection
            # Every time too: a newer LangGraph's checkpointer migrations, for every store.
            from aios import checkpoints

            checkpoints.set_up_store(pg)
            _grant_writer(pg, names)
            _apply_deny_list(pg, names)
            conn.commit()
            on_history = [r for r in at if migrations.is_history_revision(r)]
            (on_aios,) = [r for r in at if not migrations.is_history_revision(r)]
            return Revisions(on_aios, on_history[0] if on_history else None)
    finally:
        engine.dispose()


def _migrate(run: _Run) -> None:
    run.revisions = upgrade_store(run.names, history=run.history)


def _fill_people(run: _Run) -> None:
    """Fill the store's people mirror from its organisation's members (`aios.datastores.people`):
    a new store's, before it's ready."""
    _sync_people(run.store_id, run.names)


def _sync_people(store_id: uuid.UUID, names: StoreNames) -> None:
    from aios.datastores import people

    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == store_id)
        )
    if organisation_id is None:  # being deleted
        return
    with as_owner(names) as conn:
        people.sync(conn, organisation_id)


def _mark_ready(run: _Run) -> None:
    # No host or port: it's on the platform database's instance, which the app reaches at its
    # own address (`connections.store_url`), not necessarily the provisioner's.
    # Only if it's still the provisioner's: its organisation can be deleted while the steps
    # run (its creator accepting an invitation straight after signing up), leaving it `deleting`
    # with none, for the deletion sweep, which waits for this run's lock.
    with SessionLocal() as db:
        marked = db.execute(
            update(DataStore)
            .where(DataStore.id == run.store_id, DataStore.status.in_(PROVISIONABLE))
            .values(
                status=DataStoreStatus.ready,
                schema_revision=run.revisions.aios,
                history_revision=run.revisions.history,
                error=None,
            )
        )
        db.commit()
    run.ready = marked.rowcount == 1


# In order. Each can be run again, whatever the ones before it left.
STEPS: list[Callable[[_Run], None]] = [
    _create_roles,
    _create_database,
    _restrict_connect,
    _set_up_database,
    _migrate,
    _fill_people,
    _mark_ready,
]


@contextmanager
def provisioner_locked(
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
    gone, or that's past provisioning (or goes `deleting` while it runs), or, unless `wait`,
    that another run is on."""
    with provisioner_locked(store_id, wait) as server:
        if server is None:
            return None
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status not in PROVISIONABLE:
                return None
            names = StoreNames.of(store)
            history = store.export_run_history
        passwords = _passwords(store_id)
        if passwords is None:
            return None
        run = _Run(store_id, names, *passwords, server=server, history=history)
        for step in STEPS:
            step(run)
    if run.revisions is None:  # a test ran only some of the steps
        return None
    if not run.ready:
        log.info("Data store %s is being deleted: provisioned, but not marked ready", store_id)
        return None
    log.info("Data store %s is ready at revisions %s", store_id, run.revisions)
    return run.revisions.aios


def drop(names: StoreNames) -> None:
    """Drop a store's database and roles, if they exist: deleting a store (`delete_store`), and
    tests."""
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
        for role in (names.rw, names.ro, names.writer, names.owner):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@dataclass
class _Deletion:
    """What the steps of deleting one store share."""

    store_id: uuid.UUID
    names: StoreNames


def _dispose_engines(deletion: _Deletion) -> None:
    # This process's: another's pool on it is cut off by the drop, and can't be reopened (the
    # store isn't ready).
    for role in ("rw", "ro"):
        connections.dispose_engine(deletion.store_id, role)


def _drop(deletion: _Deletion) -> None:
    drop(deletion.names)


def _delete_files(deletion: _Deletion) -> None:
    files.delete_under(files.store_prefix(deletion.store_id))


def _mark_deleted(deletion: _Deletion) -> None:
    # Its run history outbox goes too: there's nowhere left to export to.
    with SessionLocal() as db:
        db.execute(delete(HistoryExport).where(HistoryExport.data_store_id == deletion.store_id))
        db.execute(
            update(DataStore)
            .where(DataStore.id == deletion.store_id, DataStore.status == DataStoreStatus.deleting)
            .values(status=DataStoreStatus.deleted, error=None)
        )
        db.commit()


# In order. Each can be run again, whatever the ones before it left.
DELETE_STEPS: list[Callable[[_Deletion], None]] = [
    _dispose_engines,
    _drop,
    _delete_files,
    _mark_deleted,
]


def delete_store(store_id: uuid.UUID, wait: bool = True) -> bool:
    """Drop a `deleting` store's database, roles and files and mark it `deleted` (see the
    module docstring); returns whether it did. Does nothing for a store that's gone or isn't
    being deleted, or, unless `wait`, that another run is on. A provisioning run waits for
    this, and then finds the store isn't for it."""
    with provisioner_locked(store_id, wait) as server:
        if server is None:
            return False
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status != DataStoreStatus.deleting:
                return False
            deletion = _Deletion(store_id, StoreNames.of(store))
        for step in DELETE_STEPS:
            step(deletion)
    log.info("Data store %s is deleted", store_id)
    return True


def delete_deleting(
    store_ids: Collection[uuid.UUID] | None = None, wait: bool = False
) -> dict[uuid.UUID, bool | BaseException]:
    """Delete every store that's `deleting` (`delete_store`), one at a time; returns, for each
    it got to, True, or what stopped it (recorded on the store, which the next sweep tries
    again). Unless `wait`, a store another run is on is left to the next sweep. The
    provisioning worker runs it when an organisation is deleted (for its store, waiting for a
    provisioning run still on it), and every `aios.worker.DELETE_STORES_EVERY`.

    `store_ids` limits it to those stores (tests use it to leave other stores alone)."""
    outcome: dict[uuid.UUID, bool | BaseException] = {}
    with SessionLocal() as db:
        deleting = db.scalars(
            select(DataStore.id).where(
                _only(store_ids), DataStore.status == DataStoreStatus.deleting
            )
        ).all()
    for store_id in deleting:
        try:
            if delete_store(store_id, wait):
                outcome[store_id] = True
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - one store's failure is reported, not raised
            log.exception("Deleting data store %s failed", store_id)
            _record_error(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


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


def migrate_store(store_id: uuid.UUID, wait: bool = True) -> str | None:
    """Bring a ready store to the tree's head (and its `history` line, if it has it or its
    owner wants it), and its roles to the limits configured now, and record its revisions;
    returns its `aios` line's. Unless `wait`, does nothing (returns None) if another run is on
    it."""
    with provisioner_locked(store_id, wait) as server:
        if server is None:
            return None
        with SessionLocal() as db:
            store = db.get(DataStore, store_id)
            if store is None or store.status != DataStoreStatus.ready:
                raise LookupError(f"Data store {store_id} isn't ready")
            names = StoreNames.of(store)
            history = store.export_run_history
        _set_limits(server, names)
        _create_writer(server, names)
        revisions = upgrade_store(names, history=history)
        # A store migrated to the mirror's revision gets it filled; one that has it is caught up.
        _sync_people(store_id, names)
        with SessionLocal() as db:
            db.execute(
                update(DataStore)
                .where(DataStore.id == store_id)
                .values(
                    schema_revision=revisions.aios,
                    history_revision=revisions.history,
                    error=None,
                )
            )
            db.commit()
        return revisions.aios


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


def _migrate_ready(
    which: ColumnElement[bool], skip: Collection[uuid.UUID] = (), wait: bool = True
) -> dict[uuid.UUID, str | BaseException]:
    """Bring the ready stores `which` picks (but `skip`) to the tree's head, one at a time;
    returns the revision each reached, or what stopped it (recorded on the store). Unless
    `wait`, skips a store another run is on."""
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
            revision = migrate_store(store_id, wait)
            if revision is not None:
                outcome[store_id] = revision
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Migrating data store %s failed", store_id)
            _record_error(store_id, describe_error(exc))
            outcome[store_id] = exc
    return outcome


def _behind_head() -> ColumnElement[bool]:
    """Stores at an earlier revision of the tree than its head, on either line, or without the
    `history` line the export wants (its set-up was lost, or failed). Not one it
    doesn't know: the release after this one has already migrated that store."""
    return or_(
        DataStore.schema_revision.in_(migrations.revisions_before_head()),
        DataStore.history_revision.in_(migrations.history_revisions_before_head()),
        and_(
            DataStore.history_revision.is_(None),
            DataStore.export_run_history,
        ),
    )


def retry_unfinished(
    store_ids: Collection[uuid.UUID] | None = None,
) -> dict[uuid.UUID, str | BaseException]:
    """Try once more to provision the stores whose provisioning gave up, or hasn't finished
    (`UNFINISHED_AFTER`), and bring ready stores behind the tree's head to it (the release
    before this one made them after the deploy migrated stores, or migrating them failed); the
    provisioning worker runs it every few minutes (`aios.worker.RETRY_UNFINISHED_EVERY`).
    Returns the revision each reached, or what stopped it. A store another run is on (a
    deploy's migrate-all, say) is left to it: new stores' provisioning waits behind this, on
    the one provisioning worker."""
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
        ),
        wait=False,
    )
    return outcome | _migrate_ready(and_(only, _behind_head()), skip=outcome, wait=False)


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
