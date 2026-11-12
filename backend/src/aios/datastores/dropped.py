"""Dropped tables and columns: what an approved `drop_table` or `drop_column` leaves
(`aios.datastores.schema_changes`), until a member restores it or it's purged.

A drop renames the table or column to `_dropped_<timestamp>_<name>` and records it in
`aios.dropped_objects`. Applying it then takes the store's roles' access to it away
(`grant_access`), so it's gone from the data tools and anything else that reads the store as
them, and Postgres refuses it to `query_data` and `write_data` by name:

- A dropped table: no role but the owner has any privilege on it.
- A table with dropped columns: the readers have `SELECT` (and its writers `INSERT` and
  `UPDATE`) on its other columns only, rather than on the table, so `SELECT *` on it is refused
  until the columns are restored or purged. Such a column isn't NOT NULL any more, so rows can be
  written without it.

What reads the store's catalog to show its tables keeps out dropped ones with the predicates
here (`VISIBLE_TABLE`, `VISIBLE_COLUMN`) as well as by the roles' privileges.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.schema_changes import (
    DROPPED_PREFIX,
    SCHEMA,
    record_history,
    schema_transaction,
    table_exists,
)
from aios.db import SessionLocal
from aios.models import DataStore

if TYPE_CHECKING:
    from aios.datastores.provision import StoreNames

log = logging.getLogger(__name__)

# For catalog queries, on `pg_class c` and `pg_attribute a`: not a dropped table or column.
VISIBLE_TABLE = f"NOT starts_with(c.relname, '{DROPPED_PREFIX}')"
VISIBLE_COLUMN = f"NOT starts_with(a.attname, '{DROPPED_PREFIX}')"


def is_dropped(name: str) -> bool:
    return name.startswith(DROPPED_PREFIX)


@dataclass
class Removal:
    """What one drop would take out of the tools' reach, as the store is now: a table's rows
    and columns, or a column's rows and how many of them have a value in it. `rows` is None if
    the table isn't there (any more), or if counting took too long and there's no estimate;
    `estimated` if it's Postgres's estimate (`pg_class.reltuples`) rather than a count."""

    table: str
    column: str | None
    rows: int | None
    values: int | None
    columns: list[str] | None
    estimated: bool = False


# How long (ms) counting a table's rows for the card may take before it's estimated instead:
# a huge table, or one locked by a change being applied, mustn't hold up the member deciding.
IMPACT_COUNT_TIMEOUT = 3000


def _count(pg: psycopg.Connection, relation: sql.Identifier, column: str | None) -> Removal:
    counted = sql.SQL("count({})").format(sql.Identifier(column)) if column else sql.NULL
    pg.execute("SAVEPOINT impact")
    try:
        pg.execute("SELECT set_config('statement_timeout', %s, true)", (str(IMPACT_COUNT_TIMEOUT),))
        rows, values = pg.execute(
            sql.SQL("SELECT count(*), {} FROM {}").format(counted, relation)
        ).fetchone()
        pg.execute("RELEASE SAVEPOINT impact")
        return Removal("", column, rows, values, None)
    except psycopg.errors.QueryCanceled:
        pg.execute("ROLLBACK TO SAVEPOINT impact")
    (estimate,) = pg.execute(
        "SELECT reltuples FROM pg_class WHERE oid = to_regclass(%s)", (relation.as_string(pg),)
    ).fetchone()
    # -1: never vacuumed or analyzed, so no estimate.
    return Removal("", column, round(estimate) if estimate >= 0 else None, None, None, True)


def impact(data_store_id: uuid.UUID, ops: list[dict[str, Any]]) -> list[Removal]:
    """What a request's drops would remove, read as the store's read-only role, for the member
    deciding it."""
    drops = [op for op in ops if op["op"] in ("drop_table", "drop_column")]
    if not drops:
        return []
    out: list[Removal] = []
    with store_session(data_store_id, "ro") as session:
        pg: psycopg.Connection = session.connection().connection.driver_connection
        for op in drops:
            table, column = op["table"], op.get("column")
            relation = sql.Identifier(SCHEMA, table)
            columns = [c for (c,) in pg.execute(_READABLE_SQL, (relation.as_string(pg),))]
            if not columns or (column is not None and column not in columns):
                out.append(Removal(table, column, None, None, None))
                continue
            removal = _count(pg, relation, column)
            removal.table = table
            removal.columns = None if column else columns
            out.append(removal)
    return out


# A `data` table's columns the role reading it may read, but none that are dropped.
_READABLE_SQL = f"""
    SELECT a.attname FROM pg_attribute a
    WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
      AND has_column_privilege(a.attrelid, a.attnum, 'SELECT') AND {VISIBLE_COLUMN}
    ORDER BY a.attnum
"""

_COLUMNS_SQL = """
    SELECT a.attname FROM pg_attribute a
    WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""


# Who may write a `data` table, as the store records it (`aios.datastores.ownership`): its owner,
# whether the owner is archived, and the agents it's shared with that aren't.
_WRITERS_SQL = """
    SELECT o.agent_id, a.archived_at IS NOT NULL,
           ARRAY(SELECT s.agent_id FROM aios.table_shares s
                 JOIN aios.agents sa ON sa.id = s.agent_id
                 WHERE s.table_name = o.table_name AND sa.archived_at IS NULL ORDER BY s.agent_id)
    FROM aios.table_owners o LEFT JOIN aios.agents a ON a.id = o.agent_id
    WHERE o.table_name = %s
"""


def agent_roles_present(
    conn: psycopg.Connection, names: "StoreNames", live: bool = False
) -> set[str]:
    """The store's agents' login roles there are now (`aios.datastores.agent_roles`); if
    `live`, only those that may log in (not an archived agent's)."""
    return {
        name
        for (name,) in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE %s AND (rolcanlogin OR NOT %s)",
            (names.agent_pattern, live),
        )
    }


def writers_of(conn: psycopg.Connection, names: "StoreNames", table: str) -> list[str]:
    """The roles that may write `data.<table>` (not a dropped one), by its ownership records:
    the agents' group if nobody owns it (any agent with the write tool); none if its owner is
    archived; otherwise its owner's role and those of the agents it's shared with (that aren't
    archived). Only roles that exist and may log in: one still being made gets its grants once
    it is."""
    row = conn.execute(_WRITERS_SQL, (table,)).fetchone()
    if row is None or row[0] is None:
        return [names.group]
    owner, archived, shared = row
    if archived:
        return []
    present = agent_roles_present(conn, names, live=True)
    return [r for r in (names.agent(a) for a in (owner, *shared)) if r in present]


def grant_access(conn: psycopg.Connection, names: "StoreNames", table: str) -> None:
    """Give the store's roles the access to `data.<table>` its ownership and drops call for now
    (the one derivation every change goes through: applying a schema change, a drop, restore or
    purge, and applying agents' access after a share, transfer or archive):

    - Reads: `rw`, `ro` and the agents' group (so every agent) read it.
    - Writes: `writers_of` it, and `rw` (until it loses its writes to data tables, which only
      agents' logins will make).
    - None of it if it's dropped; only its other columns if it has dropped columns.

    Run as the owner, in the transaction that changed it."""
    relation = sql.Identifier(SCHEMA, table)
    agents = sorted(agent_roles_present(conn, names))
    everyone = sql.SQL(", ").join(
        sql.Identifier(r) for r in (names.rw, names.ro, names.group, *agents)
    )
    # Takes its column privileges too.
    conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(relation, everyone))
    if is_dropped(table):
        return
    readers = sql.SQL(", ").join(sql.Identifier(r) for r in (names.rw, names.ro, names.group))
    writers = [names.rw, *writers_of(conn, names, table)]
    can_write = sql.SQL(", ").join(sql.Identifier(r) for r in writers)
    qualified = sql.Identifier(SCHEMA, table).as_string(conn)
    columns = [name for (name,) in conn.execute(_COLUMNS_SQL, (qualified,))]
    kept = [c for c in columns if not is_dropped(c)]
    if kept == columns:
        conn.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(relation, readers))
        conn.execute(
            sql.SQL("GRANT INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(relation, can_write)
        )
        return
    listed = sql.SQL(", ").join(map(sql.Identifier, kept))
    # And where each row is (`tableoid`, `ctid`), which the Data page's Tables key rows by: a
    # table-wide `SELECT` would give those, but no column grant does unless they're named.
    conn.execute(
        sql.SQL("GRANT SELECT ({}, tableoid, ctid) ON TABLE {} TO {}").format(
            listed, relation, readers
        )
    )
    conn.execute(
        sql.SQL("GRANT INSERT ({0}), UPDATE ({0}) ON TABLE {1} TO {2}").format(
            listed, relation, can_write
        )
    )
    conn.execute(sql.SQL("GRANT DELETE ON TABLE {} TO {}").format(relation, can_write))


# --- The Dropped section, and restoring ---------------------------------------------------------


@dataclass
class Dropped:
    """A row of `aios.dropped_objects`: a dropped table (`column` None), or a column."""

    id: int
    # A table: its name before it was dropped. A column: the table it's in, as named now.
    table: str
    column: str | None
    dropped_name: str
    not_null: bool
    dropped_at: datetime
    restore_error: str | None

    @property
    def kind(self) -> str:
        return "table" if self.column is None else "column"


_FIELDS = "id, table_name, column_name, dropped_name, not_null, dropped_at, restore_error"


def _row(row: tuple) -> Dropped:
    return Dropped(*row)


def purge_at(item: Dropped) -> datetime:
    """When it's purged for good (`purge`)."""
    return item.dropped_at + timedelta(days=get_settings().data_store_dropped_retention_days)


def listing(data_store_id: uuid.UUID) -> list[tuple[Dropped, str | None]]:
    """What's dropped, newest first, each with why it can't be restored now, if it can't
    (`blocked`); read as the store's read-only role. A dropped table's dropped columns go with
    it, so aren't listed by themselves."""
    with store_session(data_store_id, "ro") as session:
        pg: psycopg.Connection = session.connection().connection.driver_connection
        rows = pg.execute(
            f"SELECT {_FIELDS} FROM aios.dropped_objects"
            " WHERE column_name IS NULL OR NOT starts_with(table_name, %s)"
            " ORDER BY dropped_at DESC, id DESC",
            (DROPPED_PREFIX,),
        )
        return [(item, blocked(pg, item)) for item in map(_row, rows.fetchall())]


def find(data_store_id: uuid.UUID, dropped_id: int) -> tuple[Dropped, str | None] | None:
    """One dropped table or column, with why it can't be restored now (`blocked`), if it
    can't; None if there's no such thing (restored, purged, or never)."""
    with store_session(data_store_id, "ro") as session:
        pg: psycopg.Connection = session.connection().connection.driver_connection
        row = pg.execute(
            f"SELECT {_FIELDS} FROM aios.dropped_objects WHERE id = %s", (dropped_id,)
        ).fetchone()
        return None if row is None else (item := _row(row), blocked(pg, item))


def blocked(pg: psycopg.Connection, item: Dropped) -> str | None:
    """Why it can't be restored now, if it can't: its name has been taken since, or (a column)
    its table is dropped too."""
    if item.column is None:
        if table_exists(pg, item.table):
            return (
                f"`{SCHEMA}.{item.table}` exists again: a table of that name was made after this"
                " one was dropped, so this one can't take its name back."
            )
        return None
    if is_dropped(item.table):
        table = pg.execute(
            "SELECT table_name FROM aios.dropped_objects"
            " WHERE column_name IS NULL AND dropped_name = %s",
            (item.table,),
        ).fetchone()
        return f"Restore the table `{table[0] if table else item.table}` first: it's dropped too."
    if not table_exists(pg, item.table):
        return f"There's no table `{SCHEMA}.{item.table}` to restore it to."
    qualified = sql.Identifier(SCHEMA, item.table).as_string(pg)
    taken = pg.execute(
        "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attname = %s"
        " AND NOT attisdropped",
        (qualified, item.column),
    ).fetchone()
    if taken:
        return (
            f"`{SCHEMA}.{item.table}` already has a column `{item.column}` again: one was added"
            " after this one was dropped, so this one can't take its name back."
        )
    return None


def _restore_sql(pg: psycopg.Connection, item: Dropped) -> tuple[list[sql.Composable], str, str]:
    """The statements that restore it, what they're summed up as, and the table to grant
    access to afterwards."""
    q = sql.Identifier
    if item.column is None:
        old, new = sql.Literal(item.dropped_name), sql.Literal(item.table)
        return (
            [
                sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    q(SCHEMA, item.dropped_name), q(item.table)
                ),
                sql.SQL(
                    "UPDATE aios.embedding_columns SET table_name = {} WHERE table_name = {}"
                ).format(new, old),
                sql.SQL(
                    "UPDATE aios.dropped_objects SET table_name = {}"
                    " WHERE table_name = {} AND column_name IS NOT NULL"
                ).format(new, old),
                # Its owner and shares (`aios.datastores.ownership`), which a share's foreign
                # key carries along.
                sql.SQL(
                    "UPDATE aios.table_owners SET table_name = {} WHERE table_name = {}"
                ).format(new, old),
            ],
            f"Restore table {SCHEMA}.{item.table}",
            item.table,
        )
    table = q(SCHEMA, item.table)
    statements: list[sql.Composable] = [
        sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(
            table, q(item.dropped_name), q(item.column)
        ),
        sql.SQL(
            "UPDATE aios.embedding_columns SET column_name = {}"
            " WHERE table_name = {} AND column_name = {}"
        ).format(sql.Literal(item.column), sql.Literal(item.table), sql.Literal(item.dropped_name)),
    ]
    summary = f"Restore column {item.column} of {SCHEMA}.{item.table}"
    if item.not_null:
        # Unless rows were written without it while it was dropped.
        empty = pg.execute(
            sql.SQL("SELECT EXISTS (SELECT FROM {} WHERE {} IS NULL)").format(
                table, q(item.dropped_name)
            )
        ).fetchone()[0]
        if empty:
            summary += " (nullable now: rows were written without it while it was dropped)"
        else:
            statements.append(
                sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET NOT NULL").format(table, q(item.column))
            )
    return statements, summary, item.table


class _Refused(Exception):
    pass


def _record(conn: psycopg.Connection, summary: str, statements: list[sql.Composable]) -> None:
    record_history(conn, summary, "\n".join(s.as_string(conn) + ";" for s in statements))


def restore(data_store_id: uuid.UUID, dropped_id: int) -> str | None:
    """Rename a dropped table or column back, give the login roles their access to it again,
    and record it in `aios.schema_history`; on the urgent worker, as the store's owner.
    Returns why it was refused (also recorded on it, for the Dropped section), or None: done,
    or there's nothing to restore (already restored, or purged)."""
    from aios.datastores.provision import StoreNames, as_owner, provisioner_locked

    with SessionLocal() as db:
        names = StoreNames.of(db.get(DataStore, data_store_id))
    with provisioner_locked(data_store_id), as_owner(names) as conn:
        try:
            with schema_transaction(conn):
                row = conn.execute(
                    f"SELECT {_FIELDS} FROM aios.dropped_objects WHERE id = %s FOR UPDATE",
                    (dropped_id,),
                ).fetchone()
                if row is None:
                    return None
                item = _row(row)
                why = blocked(conn, item)
                if why is not None:
                    raise _Refused(why)
                statements, summary, table = _restore_sql(conn, item)
                statements.append(
                    sql.SQL("DELETE FROM aios.dropped_objects WHERE id = {}").format(
                        sql.Literal(item.id)
                    )
                )
                for statement in statements:
                    conn.execute(statement)
                grant_access(conn, names, table)
                _record(conn, summary, statements)
        except _Refused as exc:
            conn.execute(
                "UPDATE aios.dropped_objects SET restore_error = %s WHERE id = %s",
                (str(exc), dropped_id),
            )
            return str(exc)
    log.info("Restored dropped %s %s in data store %s", item.kind, dropped_id, data_store_id)
    return None


# --- Purging ------------------------------------------------------------------------------------


def _purge_sql(item: Dropped) -> tuple[list[sql.Composable], str, str | None]:
    """The statements that purge it for good, what they're summed up as, and the table whose
    access to grant afterwards (a column's)."""
    q, lit = sql.Identifier, sql.Literal
    if item.column is None:
        name = lit(item.dropped_name)
        return (
            [
                sql.SQL("DROP TABLE {}").format(q(SCHEMA, item.dropped_name)),
                sql.SQL("DELETE FROM aios.embedding_columns WHERE table_name = {}").format(name),
                # Its owner and shares (`aios.datastores.ownership`).
                sql.SQL("DELETE FROM aios.table_owners WHERE table_name = {}").format(name),
                # With its own dropped columns.
                sql.SQL(
                    "DELETE FROM aios.dropped_objects"
                    " WHERE id = {} OR (column_name IS NOT NULL AND table_name = {})"
                ).format(lit(item.id), name),
            ],
            f"Purge dropped table {SCHEMA}.{item.table}",
            None,
        )
    return (
        [
            sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(
                q(SCHEMA, item.table), q(item.dropped_name)
            ),
            sql.SQL(
                "DELETE FROM aios.embedding_columns WHERE table_name = {} AND column_name = {}"
            ).format(lit(item.table), lit(item.dropped_name)),
            sql.SQL("DELETE FROM aios.dropped_objects WHERE id = {}").format(lit(item.id)),
        ],
        f"Purge dropped column {item.column} of {SCHEMA}.{item.table}",
        item.table,
    )


def purge(data_store_id: uuid.UUID, wait: bool = True) -> list[str]:
    """Drop for good the store's tables and columns dropped longer ago than
    `DATA_STORE_DROPPED_RETENTION_DAYS`, each in a transaction of its own, recorded in
    `aios.schema_history`; on the urgent worker, as the store's owner. Returns what it
    purged. Unless `wait`, does nothing if another run (a migration, say) is on the store."""
    from aios.datastores.provision import StoreNames, as_owner, provisioner_locked

    with SessionLocal() as db:
        names = StoreNames.of(db.get(DataStore, data_store_id))
    retention = timedelta(days=get_settings().data_store_dropped_retention_days)
    purged: list[str] = []
    with provisioner_locked(data_store_id, wait) as server:
        if server is None:
            return purged
        with as_owner(names) as conn:
            # Tables first: their dropped columns go with them.
            expired = conn.execute(
                "SELECT id FROM aios.dropped_objects WHERE dropped_at <= now() - %s"
                " ORDER BY column_name IS NOT NULL, dropped_at, id",
                (retention,),
            ).fetchall()
            for (dropped_id,) in expired:
                with schema_transaction(conn):
                    row = conn.execute(
                        f"SELECT {_FIELDS} FROM aios.dropped_objects WHERE id = %s FOR UPDATE",
                        (dropped_id,),
                    ).fetchone()
                    if row is None:  # went with its table
                        continue
                    statements, summary, table = _purge_sql(_row(row))
                    for statement in statements:
                        conn.execute(statement)
                    if table is not None and table_exists(conn, table):
                        grant_access(conn, names, table)
                    _record(conn, summary, statements)
                purged.append(summary)
    if purged:
        log.info("Purged %d dropped objects in data store %s", len(purged), data_store_id)
    return purged


def purge_all() -> dict[uuid.UUID, list[str] | BaseException]:
    """Purge what's past retention in every ready store, one at a time (`purge`); returns what
    each purged, or what stopped it, without stopping the rest. A store another run is on is
    left for next time."""
    from celery.exceptions import SoftTimeLimitExceeded
    from sqlalchemy import select

    from aios.datastores import migrations
    from aios.models import DataStoreStatus

    with SessionLocal() as db:
        ready = db.scalars(
            select(DataStore.id)
            .where(
                DataStore.status == DataStoreStatus.ready,
                # Not one behind the tree's head: it may not have `aios.dropped_objects` yet.
                DataStore.schema_revision.not_in(migrations.revisions_before_head()),
            )
            .order_by(DataStore.created_at)
        ).all()
    outcome: dict[uuid.UUID, list[str] | BaseException] = {}
    for store_id in ready:
        try:
            outcome[store_id] = purge(store_id, wait=False)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - one store's failure is reported, not raised
            log.exception("Purging dropped objects in data store %s failed", store_id)
            outcome[store_id] = exc
    return outcome
