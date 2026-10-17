"""Dropped tables and columns: what an approved `drop_table` or `drop_column` leaves
(`aios.datastores.schema_changes`), until the owner restores it or it's purged.

A drop renames the table or column to `_dropped_<timestamp>_<name>` and records it in
`aios.dropped_objects`. Applying it then takes the store's login roles' access to it away
(`grant_access`), so it's gone from the data tools and anything else that reads the store as
them, and Postgres refuses it to `query_data` and `write_data` by name:

- A dropped table: neither role has any privilege on it.
- A table with dropped columns: both roles have `SELECT` (and `rw` `INSERT` and `UPDATE`) on
  its other columns only, rather than on the table, so `SELECT *` on it is refused until the
  columns are restored or purged. Such a column isn't NOT NULL any more, so rows can be
  written without it.

What reads the store's catalog to show its tables keeps out dropped ones with the predicates
here (`VISIBLE_TABLE`, `VISIBLE_COLUMN`) as well as by the roles' privileges.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.schema_changes import DROPPED_PREFIX, SCHEMA
from aios.db import SessionLocal
from aios.models import DataStore

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
    the table isn't there (any more)."""

    table: str
    column: str | None
    rows: int | None
    values: int | None
    columns: list[str] | None


def impact(data_store_id: uuid.UUID, ops: list[dict[str, Any]]) -> list[Removal]:
    """What a request's drops would remove, read as the store's read-only role, for the owner
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
            qualified = relation.as_string(pg)
            columns = [c for (c,) in pg.execute(_READABLE_SQL, (qualified,))]
            if not columns or (column is not None and column not in columns):
                out.append(Removal(table, column, None, None, None))
                continue
            if column is None:
                (rows,) = pg.execute(sql.SQL("SELECT count(*) FROM {}").format(relation)).fetchone()
                out.append(Removal(table, None, rows, None, columns))
            else:
                rows, values = pg.execute(
                    sql.SQL("SELECT count(*), count({}) FROM {}").format(
                        sql.Identifier(column), relation
                    )
                ).fetchone()
                out.append(Removal(table, column, rows, values, None))
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


def grant_access(conn: psycopg.Connection, rw: str, ro: str, table: str) -> None:
    """Give the store's login roles (`rw`, `ro`) the access to `data.<table>` it calls for now:
    none if it's dropped; only to its other columns if it has dropped columns; otherwise what
    the store's default privileges give a new table. Run as the owner, in the transaction that
    changed it."""
    relation = sql.Identifier(SCHEMA, table)
    both = sql.SQL("{}, {}").format(sql.Identifier(rw), sql.Identifier(ro))
    # Takes its column privileges too.
    conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(relation, both))
    if is_dropped(table):
        return
    qualified = sql.Identifier(SCHEMA, table).as_string(conn)
    columns = [name for (name,) in conn.execute(_COLUMNS_SQL, (qualified,))]
    kept = [c for c in columns if not is_dropped(c)]
    if kept == columns:
        conn.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(relation, both))
        conn.execute(
            sql.SQL("GRANT INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
                relation, sql.Identifier(rw)
            )
        )
        return
    listed = sql.SQL(", ").join(map(sql.Identifier, kept))
    conn.execute(sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(listed, relation, both))
    conn.execute(
        sql.SQL("GRANT INSERT ({0}), UPDATE ({0}) ON TABLE {1} TO {2}").format(
            listed, relation, sql.Identifier(rw)
        )
    )
    conn.execute(sql.SQL("GRANT DELETE ON TABLE {} TO {}").format(relation, sql.Identifier(rw)))


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


def _exists(pg: psycopg.Connection, table: str) -> bool:
    qualified = sql.Identifier(SCHEMA, table).as_string(pg)
    return pg.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,)).fetchone()[0]


def blocked(pg: psycopg.Connection, item: Dropped) -> str | None:
    """Why it can't be restored now, if it can't: its name has been taken since, or (a column)
    its table is dropped too."""
    if item.column is None:
        if _exists(pg, item.table):
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
    if not _exists(pg, item.table):
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


@contextmanager
def _schema_transaction(conn: psycopg.Connection) -> Iterator[None]:
    """A transaction (as the owner) that changes the schema as applying a schema change does:
    waiting for its locks no longer than `SCHEMA_CHANGE_LOCK_TIMEOUT`, one at a time with the
    others (`aios.schema_history` is locked), and recorded there, bumping the revision."""
    with conn.transaction():
        timeout = f"{get_settings().schema_change_lock_timeout}ms"
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (timeout,))
        conn.execute("LOCK TABLE aios.schema_history IN EXCLUSIVE MODE")
        yield


def _record(conn: psycopg.Connection, summary: str, statements: list[sql.Composable]) -> None:
    text = "\n".join(s.as_string(conn) + ";" for s in statements)
    conn.execute("INSERT INTO aios.schema_history (summary, sql) VALUES (%s, %s)", (summary, text))


def restore(data_store_id: uuid.UUID, dropped_id: int) -> str | None:
    """Rename a dropped table or column back, give the login roles their access to it again,
    and record it in `aios.schema_history`; on the provisioning worker, as the store's owner.
    Returns why it was refused (also recorded on it, for the Dropped section), or None: done,
    or there's nothing to restore (already restored, or purged)."""
    from aios.datastores.provision import StoreNames, as_owner, provisioner_locked

    with SessionLocal() as db:
        names = StoreNames.of(db.get(DataStore, data_store_id))
    with provisioner_locked(data_store_id), as_owner(names) as conn:
        try:
            with _schema_transaction(conn):
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
                grant_access(conn, names.rw, names.ro, table)
                _record(conn, summary, statements)
        except _Refused as exc:
            conn.execute(
                "UPDATE aios.dropped_objects SET restore_error = %s WHERE id = %s",
                (str(exc), dropped_id),
            )
            return str(exc)
    log.info("Restored dropped %s %s in data store %s", item.kind, dropped_id, data_store_id)
    return None
