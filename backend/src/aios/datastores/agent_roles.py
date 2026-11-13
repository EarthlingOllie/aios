"""Agents' own database logins: the database itself enforces who may write each `data` table.

Each agent that may write (it has the `write_data` tool) gets a login role in its organisation's
store, `<prefix>_agent_<agent id>` (`provision.StoreNames.agent`), which `write_data` writes as
(`connections.agent_session`). Its password is generated here, kept encrypted in the platform's
`agent_store_roles` (`aios.models.AgentStoreRole`) like stores' own, and reaches Postgres only
as a SCRAM verifier. Each login:

- is a member of the store's agents' group (`<prefix>_agents`), inheriting it but unable to
  `SET ROLE` to it, and of nothing else; it can't create anything, nor change its own limits;
- may connect to its own store only, with its own connection limit
  (`data_store_agent_connection_limit`) and the store's statement and idle timeouts;
- writes exactly the `data` tables it owns or is shared (`dropped.grant_access`), while the
  group reads the whole store but the deny list, writes the tables nobody owns, and inserts into
  an explicit list of platform tables (`provision.grant_group`, `provision.AGENT_WRITES`). It
  never writes conversation content or memory.

Model-written SQL (`query_data`) stays on the store's read-only role.

**Applying.** Making roles and changing grants needs the provisioner, so it's a task on the
urgent maintenance lane (`aios.worker.apply_agent_access_task`, queued by `enqueue`): after a
share, unshare or transfer, an archive or unarchive, an agent given the write tool, and a write
that found its role missing or refused. `apply` re-derives everything from the store's records
each time, so it can be run again whatever went before; changes to a table's access show as
pending (`aios.table_owners.access_pending_since`) until it has. Schema changes, drops, restores
and purges set their tables' grants in their own transactions (`dropped.grant_access`), and the
store migration command applies and then verifies every store's (`verify`).

**Archiving** an agent takes away its login, revokes its grants and ends its connections;
unarchiving gives back the same login, with its grants re-derived.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql
from sqlalchemy import select, update

from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import AgentStoreRole, DataStore, DataStoreStatus

log = logging.getLogger(__name__)

# The tool whose agents get a login.
WRITE_TOOL = "write_data"

# The agents that may write: those whose current version has the write tool.
_WRITING_AGENTS_SQL = """
    SELECT a.id, a.archived_at IS NOT NULL
    FROM aios.agents a
    WHERE EXISTS (SELECT FROM aios.agent_version_tools t
                  WHERE t.agent_version_id = a.current_version_id AND t.tool_key = %s)
    ORDER BY a.id
"""


@dataclass(frozen=True)
class Login:
    agent_id: int
    role: str
    # Archived, or no longer has the write tool: no login, no grants.
    live: bool


def enqueue(data_store_id: uuid.UUID) -> None:
    """Queue `apply` for the store on the urgent lane. A broker that's down is logged, not
    raised: the change stays pending, and the next write that needs it queues it again."""
    from aios.worker import apply_agent_access_task

    try:
        apply_agent_access_task.delay(str(data_store_id))
    except Exception:  # noqa: BLE001 - the change is recorded; applying it can wait
        log.exception("Couldn't queue applying agents' access in data store %s", data_store_id)


def _passwords(data_store_id: uuid.UUID, logins: list[Login]) -> dict[int, str]:
    """Each login's password, generating (and recording) those the store has none for."""
    with SessionLocal() as db:
        rows = {
            r.agent_id: r
            for r in db.scalars(
                select(AgentStoreRole).where(AgentStoreRole.data_store_id == data_store_id)
            )
        }
        for login in logins:
            row = rows.get(login.agent_id)
            if row is None:
                row = AgentStoreRole(
                    data_store_id=data_store_id,
                    agent_id=login.agent_id,
                    role_name=login.role,
                    password=credentials.encrypt(credentials.new_password()),
                )
                db.add(row)
                rows[login.agent_id] = row
            row.dropped_at = None
        db.commit()
        return {a: credentials.decrypt(r.password) for a, r in rows.items()}


@contextmanager
def _access_locked(data_store_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """The provisioner on the platform database, holding the store's access lock: runs of
    `apply` on a store wait for each other (not for migrations: their own lock)."""
    from aios.datastores.provision import CONNECT_TIMEOUT

    url = get_settings().data_store_provisioner_url
    with psycopg.connect(
        libpq_conninfo(url), autocommit=True, connect_timeout=CONNECT_TIMEOUT
    ) as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (f"aios.data_store_access:{data_store_id}",),
        )
        yield conn


def _exists(server: psycopg.Connection, role: str) -> bool:
    return (
        server.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None
    )


def _set_login(server: psycopg.Connection, names, login: Login, password: str) -> None:
    """Make the agent's role as `login` says, if it isn't there, and set what it is: its login
    (or none), password verifier, connection limit and timeouts, its one membership, and
    whether it may connect to the store."""
    settings = get_settings()
    role = sql.Identifier(login.role)
    if not _exists(server, login.role):
        # NOINHERIT: it inherits only what it's granted with INHERIT TRUE, its group.
        server.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(role))
    # The verifier, never the password; set every time, so a password the role changed for
    # itself is put back.
    server.execute(
        sql.SQL("ALTER ROLE {} WITH {} NOINHERIT CONNECTION LIMIT {} PASSWORD {}").format(
            role,
            sql.SQL("LOGIN" if login.live else "NOLOGIN"),
            sql.Literal(settings.data_store_agent_connection_limit),
            sql.Literal(credentials.scram_verifier(password)),
        )
    )
    # Whatever settings it gave itself (any role may set its own) go.
    server.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(role))
    server.execute(
        sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(role, sql.Identifier(names.dbname))
    )
    for setting, value in (
        ("statement_timeout", settings.data_store_statement_timeout),
        ("idle_in_transaction_session_timeout", settings.data_store_idle_in_transaction_timeout),
    ):
        server.execute(
            sql.SQL("ALTER ROLE {} SET {} = {}").format(
                role, sql.Identifier(setting), sql.Literal(value)
            )
        )
    group, db = sql.Identifier(names.group), sql.Identifier(names.dbname)
    if login.live:
        server.execute(sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(group, role))
        server.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
    else:
        server.execute(sql.SQL("REVOKE {} FROM {}").format(group, role))
        server.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(db, role))


def _disconnect(server: psycopg.Connection, roles: list[str]) -> None:
    """End the roles' sessions (an archived agent's): ending a role's takes its privileges,
    which the provisioner has only ADMIN of, from creating it."""
    for role in roles:
        server.execute(
            sql.SQL("GRANT {} TO CURRENT_USER WITH INHERIT TRUE, SET FALSE").format(
                sql.Identifier(role)
            )
        )
    server.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = ANY(%s)",
        (roles,),
    )


def logins(conn: psycopg.Connection, names, known: set[int] = frozenset()) -> list[Login]:
    """The logins the store's records call for: one per agent with the write tool, live unless
    it's archived; and the agents in `known` (that had one) that no longer have the tool, not
    live. Read as the owner."""
    writing = {a: archived for a, archived in conn.execute(_WRITING_AGENTS_SQL, (WRITE_TOOL,))}
    ids = sorted(set(writing) | set(known))
    return [Login(a, names.agent(a), a in writing and not writing[a]) for a in ids]


def apply(data_store_id: uuid.UUID) -> bool:
    """Make the store's agents' logins and derive every grant from its records (see the module
    docstring); returns whether it did (not for a store that isn't ready). Clears the pending
    flag of each change it found."""
    from aios.datastores import connections
    from aios.datastores.provision import StoreNames, as_owner, grant_group
    from aios.datastores.schema_changes import schema_transaction

    with SessionLocal() as db:
        store = db.get(DataStore, data_store_id)
        if store is None or store.status != DataStoreStatus.ready:
            return False
        names = StoreNames.of(store)
        known = set(
            db.scalars(
                select(AgentStoreRole.agent_id).where(AgentStoreRole.data_store_id == data_store_id)
            )
        )
    with _access_locked(data_store_id) as server:
        with as_owner(names) as conn:
            wanted = logins(conn, names, known)
            pending = conn.execute(
                "SELECT table_name, access_pending_since FROM aios.table_owners"
                " WHERE access_pending_since IS NOT NULL"
            ).fetchall()
        passwords = _passwords(data_store_id, wanted)
        if not _exists(server, names.group):
            server.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(names.group))
            )
        for login in wanted:
            _set_login(server, names, login, passwords[login.agent_id])
        with as_owner(names) as conn, schema_transaction(conn):
            from aios.datastores.provision import _apply_deny_list

            grant_group(conn, names)
            _apply_deny_list(conn, names)
            for table, since in pending:
                conn.execute(
                    "UPDATE aios.table_owners SET access_pending_since = NULL"
                    " WHERE table_name = %s AND access_pending_since = %s",
                    (table, since),
                )
        gone = [login.role for login in wanted if not login.live]
        if gone:
            _disconnect(server, gone)
    for login in wanted:
        if not login.live:
            connections.dispose_engine(data_store_id, connections.agent_key(login.agent_id))
    log.info("Applied agents' access in data store %s (%d logins)", data_store_id, len(wanted))
    return True


def verify(data_store_id: uuid.UUID) -> list[str]:
    """What in the store's roles and grants differs from what its records call for (empty if
    nothing): the store migration command's check, after `apply`."""
    from aios.datastores import dropped
    from aios.datastores.provision import StoreNames, as_owner

    with SessionLocal() as db:
        store = db.get_one(DataStore, data_store_id)
        names = StoreNames.of(store)
        known = set(
            db.scalars(
                select(AgentStoreRole.agent_id).where(AgentStoreRole.data_store_id == data_store_id)
            )
        )
    problems: list[str] = []
    limit = get_settings().data_store_agent_connection_limit
    with as_owner(names) as conn:
        wanted = logins(conn, names, known)
        group = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls,"
            " rolreplication FROM pg_roles WHERE rolname = %s",
            (names.group,),
        ).fetchone()
        if group is None or any(group):
            problems.append(f"{names.group}: missing, or may log in or has attributes")
        for login in wanted:
            row = conn.execute(
                "SELECT rolcanlogin, rolinherit, rolconnlimit, rolsuper OR rolcreaterole OR"
                " rolcreatedb OR rolbypassrls OR rolreplication"
                " FROM pg_roles WHERE rolname = %s",
                (login.role,),
            ).fetchone()
            if row is None:
                problems.append(f"{login.role}: missing")
                continue
            can_login, inherit, connlimit, privileged = row
            if can_login != login.live:
                problems.append(f"{login.role}: login is {can_login}, should be {login.live}")
            if inherit or privileged or connlimit != limit:
                problems.append(f"{login.role}: inherits, is privileged, or has another limit")
            memberships = conn.execute(
                "SELECT r.rolname, m.inherit_option, m.set_option, m.admin_option"
                " FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid"
                " WHERE m.member = to_regrole(%s)",
                (login.role,),
            ).fetchall()
            if memberships != ([(names.group, True, False, False)] if login.live else []):
                problems.append(f"{login.role}: memberships are {memberships}")
            connects = conn.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT')", (login.role, names.dbname)
            ).fetchone()[0]
            if connects != login.live:
                problems.append(f"{login.role}: may connect is {connects}")
        tables = [
            t
            for (t,) in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'data' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
            )
        ]
        insights = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = %s", (names.insights,)
        ).fetchall()
        roles = [
            names.rw,
            names.ro,
            names.group,
            *(r for (r,) in insights),
            *(login.role for login in wanted),
        ]
        for table in tables:
            writers = (
                set() if dropped.is_dropped(table) else set(dropped.writers_of(conn, names, table))
            )
            qualified = sql.Identifier("data", table).as_string(conn)
            for role in roles:
                (writes,) = conn.execute(
                    "SELECT has_any_column_privilege(%s, %s, 'INSERT')"
                    " OR has_table_privilege(%s, %s, 'DELETE')",
                    (role, qualified, role, qualified),
                ).fetchone()
                if role == names.rw:
                    expected = not dropped.is_dropped(table)
                elif role in (names.ro, names.group, names.insights):
                    expected = role in writers
                else:
                    # A live login's own grants, and its group's; none for one that isn't.
                    live = next(x.live for x in wanted if x.role == role)
                    expected = live and (role in writers or names.group in writers)
                if writes != expected:
                    problems.append(f"data.{table}: {role} may write is {writes}")
    return problems


def mark_pending(store, *, table: str | None = None, agent_id: int | None = None) -> None:
    """Record a change to who may write (the table, or every table the agent owns or is shared)
    that the grants don't carry yet, in the caller's store transaction; `enqueue` once it's
    committed."""
    from sqlalchemy import func, or_

    from aios.datastores.models import TableOwner, TableShare

    if table is not None:
        where = TableOwner.table_name == table
    else:
        where = or_(
            TableOwner.agent_id == agent_id,
            TableOwner.table_name.in_(
                select(TableShare.table_name).where(TableShare.agent_id == agent_id)
            ),
        )
    store.execute(update(TableOwner).where(where).values(access_pending_since=func.now()))


def mark_dropped(data_store_id: uuid.UUID) -> None:
    """Record that the store's agents' roles are gone (it was deleted)."""
    from sqlalchemy import func

    with SessionLocal() as db:
        db.execute(
            update(AgentStoreRole)
            .where(
                AgentStoreRole.data_store_id == data_store_id, AgentStoreRole.dropped_at.is_(None)
            )
            .values(dropped_at=func.now())
        )
        db.commit()
