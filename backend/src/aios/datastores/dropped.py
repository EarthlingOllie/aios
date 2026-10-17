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

import psycopg
from psycopg import sql

from aios.datastores.schema_changes import DROPPED_PREFIX, SCHEMA

# For catalog queries, on `pg_class c` and `pg_attribute a`: not a dropped table or column.
VISIBLE_TABLE = f"NOT starts_with(c.relname, '{DROPPED_PREFIX}')"
VISIBLE_COLUMN = f"NOT starts_with(a.attname, '{DROPPED_PREFIX}')"


def is_dropped(name: str) -> bool:
    return name.startswith(DROPPED_PREFIX)


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
