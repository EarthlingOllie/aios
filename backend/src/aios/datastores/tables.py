"""The store's own tables (its `data` schema), as the Data page shows them.

Everything here reads through `aios.datastores.reads`, as the store's read-only role, under the
same timeouts and caps as `query_data`. No SQL comes from outside: tables and columns are named,
and each name is looked up in the catalog before it's put in a statement (quoted).
"""

import json
import uuid
from dataclasses import asdict, dataclass

import psycopg
from psycopg import sql

from aios.config import get_settings
from aios.datastores.reads import read_only

SCHEMA = "data"
# Tables the planner expects this many rows of, or more, show its estimate rather than a count.
EXACT_COUNT_BELOW = 100_000
# The bytes a table's row count (and the comma after it) may take in the list, as JSON.
COUNT_ROOM = 24

# The store's tables, with their columns, in order: only the owner's (`data`), and a partitioned
# table once, not each partition.
TABLES_SQL = """
    SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS description,
           col.name AS column, col.type, col.nullable, col.description AS column_description,
           col.embedding,
           -- The planner's row estimate (-1, or NULL: never analyzed); a partitioned table's,
           -- its partitions'.
           CASE WHEN c.relkind = 'p' THEN (
               SELECT CASE WHEN bool_or(p.reltuples < 0) THEN -1 ELSE sum(p.reltuples) END
               FROM pg_partition_tree(c.oid) tree
               JOIN pg_class p ON p.oid = tree.relid
               WHERE tree.isleaf
           ) ELSE c.reltuples END AS estimate
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN LATERAL (
        SELECT a.attnum, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable, col_description(c.oid, a.attnum) AS description,
               -- pgvector's types, or a domain over one.
               bt.typname IN ('vector', 'halfvec', 'sparsevec') AS embedding
        FROM pg_attribute a
        JOIN pg_type t ON t.oid = a.atttypid
        JOIN pg_type bt ON bt.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END
        WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    ) col ON true
    WHERE n.nspname = %s
      AND c.relkind IN ('r', 'p')
      AND NOT c.relispartition
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relname, col.attnum
"""


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    description: str | None
    # A pgvector column: shown as its dimension, never its numbers.
    embedding: bool


@dataclass
class Table:
    name: str
    description: str | None
    columns: list[Column]
    row_count: int | None = None
    # Whether `row_count` is the planner's estimate (a big table), not a count.
    row_count_estimated: bool = False


def _catalog(pg: psycopg.Connection) -> dict[str, Table]:
    """The store's tables, with the planner's estimate of their rows as `row_count` (None if
    there is none)."""
    tables: dict[str, Table] = {}
    for row in pg.execute(TABLES_SQL, (SCHEMA,)):
        name, description, column, type_, nullable, column_description, embedding, estimate = row
        table = tables.get(name)
        if table is None:
            known = estimate is not None and estimate >= 0
            table = tables[name] = Table(
                name=name,
                description=description,
                columns=[],
                row_count=round(estimate) if known else None,
            )
        if column is not None:
            table.columns.append(
                Column(
                    name=column,
                    type=type_,
                    nullable=nullable,
                    description=column_description,
                    embedding=embedding,
                )
            )
    return tables


def _qualified(table: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table))


def _count(pg: psycopg.Connection, table: Table) -> None:
    """Counts the table's rows, unless the planner expects a lot of them (counting would take
    about as long as reading them all): then its estimate stands."""
    if table.row_count is not None and table.row_count >= EXACT_COUNT_BELOW:
        table.row_count_estimated = True
        return
    query = sql.SQL("SELECT count(*) FROM {}").format(_qualified(table.name))
    row = pg.execute(query).fetchone()
    table.row_count = row[0] if row else None


def list_tables(data_store_id: uuid.UUID) -> tuple[list[Table], bool]:
    """The store's tables, by name, with their columns and row counts; and whether some were
    left out, to keep the list within `query_data`'s byte cap."""
    max_bytes = get_settings().query_data_max_bytes
    with read_only(data_store_id) as pg:
        tables = list(_catalog(pg).values())
        # As many whole tables as fit, in order. Sized before they're counted, so only those
        # shown are; a count's digits can't take one past the room left for them.
        shown: list[Table] = []
        size = 2  # the list's brackets
        for table in tables:
            size += len(json.dumps(asdict(table), ensure_ascii=False).encode()) + COUNT_ROOM
            if size > max_bytes:
                break
            shown.append(table)
        for table in shown:
            _count(pg, table)
    return shown, len(shown) < len(tables)
