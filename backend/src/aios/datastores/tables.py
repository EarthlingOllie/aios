"""The store's own tables (its `data` schema), as the Data page shows them.

Everything here reads through `aios.datastores.reads`, as the store's read-only role, under the
same timeouts and caps as `query_data`. No SQL comes from outside: tables and columns are named,
and each name is looked up in the catalog before it's put in a statement (quoted).
"""

import json
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql

from aios.config import get_settings
from aios.datastores.reads import read_capped, read_only

SCHEMA = "data"
# Tables the planner expects this many rows of, or more, show its estimate rather than a count.
EXACT_COUNT_BELOW = 100_000
# The time counting a list's tables' rows may take, in all (ms).
COUNT_BUDGET_MS = 2_000
# The bytes a table's row count (and the comma after it) may take in the list, as JSON.
COUNT_ROOM = 24
# pgvector's types: shown as their dimension, never their numbers.
EMBEDDING_TYPES = ("vector", "halfvec", "sparsevec")
# The characters of each value a page of rows shows; the rest is read on asking (`read_value`).
PREVIEW_CHARS = 200
# Shorter for a table too wide for a page of them to fit the byte cap, but no shorter than this.
MIN_PREVIEW_CHARS = 16
# A row's key: its table's (or partition's) oid, and its place in it (`ctid`).
_ROW_KEY = re.compile(r"(?P<table>[0-9]{1,10}):\((?P<block>[0-9]{1,10}),(?P<place>[0-9]{1,5})\)")

# The store's tables (or the one named), with their columns, in order: only the owner's
# (`data`), and a partitioned table once, not each partition.
TABLES_SQL = """
    SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS description,
           col.name AS column, col.type, col.nullable, col.description AS column_description,
           col.base_type, col.in_array,
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
               base.typname AS base_type, base.in_array
        FROM pg_attribute a
        CROSS JOIN LATERAL (
            -- The type under any domains and arrays (e.g. `vector` under a domain over
            -- `vector[]`), and whether there was an array on the way.
            WITH RECURSIVE under (oid, depth, in_array) AS (
                SELECT a.atttypid, 0, false
                UNION ALL
                SELECT CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.typelem END,
                       under.depth + 1, under.in_array OR t.typtype <> 'd'
                FROM under
                JOIN pg_type t ON t.oid = under.oid
                WHERE t.typtype = 'd' OR (t.typcategory = 'A' AND t.typelem <> 0)
            )
            SELECT bt.typname, under.in_array
            FROM under
            JOIN pg_type bt ON bt.oid = under.oid
            ORDER BY under.depth DESC
            LIMIT 1
        ) base
        WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    ) col ON true
    WHERE n.nspname = %(schema)s
      AND (%(name)s::text IS NULL OR c.relname = %(name)s::text)
      AND c.relkind IN ('r', 'p')
      AND NOT c.relispartition
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relname, col.attnum
"""


class TableNotFound(LookupError):
    """The store has no `data` table of this name (that its read-only role may read)."""


class ColumnNotFound(LookupError):
    """The table has no column of this name."""


class CantSort(ValueError):
    """The column's type has no order to sort it by."""


class TooSlow(RuntimeError):
    """The read was stopped: it ran past the store's statement timeout (or someone cancelled
    it)."""


class RowTooWide(ValueError):
    """Even cut to the shortest preview, one row of the table is over the byte cap."""


@contextmanager
def _reading(data_store_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """`read_only`, with a read that was stopped raising `TooSlow`."""
    try:
        with read_only(data_store_id) as pg:
            yield pg
    except psycopg.errors.QueryCanceled as exc:
        # Told by its SQLSTATE, not its message, which is in the server's language.
        raise TooSlow from exc


class BadRowKey(ValueError):
    """Not a row's key (`Row.key`)."""


class RowNotFound(LookupError):
    """No row of the table has this key (any more: a row moves when it's updated)."""


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    description: str | None
    # The type's name under any domains and arrays, e.g. `jsonb` or `vector`.
    base_type: str
    # Whether it's an array (of `base_type`, maybe through domains).
    in_array: bool

    @property
    def embedding(self) -> bool:
        return self.base_type in EMBEDDING_TYPES


@dataclass
class Table:
    name: str
    description: str | None
    columns: list[Column]
    row_count: int | None = None
    # Whether `row_count` is the planner's estimate (a big table), not a count.
    row_count_estimated: bool = False

    def listed(self) -> dict:
        """The table as the list shows it (`aios.api.data.TableOut`), but for its row count."""
        return {
            "name": self.name,
            "description": self.description,
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "nullable": c.nullable,
                    "description": c.description,
                    "embedding": c.embedding,
                }
                for c in self.columns
            ],
        }

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise ColumnNotFound(name)


@dataclass
class Row:
    # Where the row is, for reading one of its values in full (`read_value`).
    key: str
    # Each value as text (None for NULL), in column order, cut to `PREVIEW_CHARS`.
    values: list[str | None]
    # The values that were cut, by their place in `values`.
    truncated: list[int]


@dataclass
class RowsPage:
    columns: list[str]
    rows: list[Row]
    # There are more rows after these.
    has_more: bool


def _catalog(pg: psycopg.Connection, name: str | None = None) -> dict[str, Table]:
    """The store's tables (or the one named), with the planner's estimate of their rows as
    `row_count` (None if there is none)."""
    tables: dict[str, Table] = {}
    for row in pg.execute(TABLES_SQL, {"schema": SCHEMA, "name": name}):
        table_name, description, column, type_, nullable, column_description = row[:6]
        base, in_array, estimate = row[6:]
        table = tables.get(table_name)
        if table is None:
            known = estimate is not None and estimate >= 0
            table = tables[table_name] = Table(
                name=table_name,
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
                    base_type=base,
                    in_array=in_array,
                )
            )
    return tables


def _table(pg: psycopg.Connection, name: str) -> Table:
    table = _catalog(pg, name).get(name)
    if table is None:
        raise TableNotFound(name)
    return table


# What reads call the table they read, so that a column named like one of their own output
# columns (`key`, `c0`, ...) is still the table's where a bare name would be the output's.
SOURCE = sql.Identifier("source")


def _qualified(table: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table))


def _count(pg: psycopg.Connection, tables: list[Table]) -> None:
    """Counts the tables' rows, unless the planner expects a lot of them (counting would take
    about as long as reading them all): then its estimate stands. The counts share a time
    budget (`COUNT_BUDGET_MS`, within the statement timeout), as a table the planner knows
    nothing of (never analyzed) may be big; a table not counted in time keeps its estimate, or
    has no count."""
    timeout = pg.execute(
        "SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'"
    ).fetchone()
    limit_ms = timeout[0] if timeout and timeout[0] > 0 else None
    deadline = time.monotonic() + COUNT_BUDGET_MS / 1000
    for table in tables:
        estimate = table.row_count
        if estimate is not None and estimate >= EXACT_COUNT_BELOW:
            table.row_count_estimated = True
            continue
        left_ms = int((deadline - time.monotonic()) * 1000)
        if left_ms > 0:
            # Undone, with the setting, by rolling back to the savepoint.
            pg.execute("SAVEPOINT count")
            try:
                budget = min(left_ms, limit_ms) if limit_ms else left_ms
                pg.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(budget)))
                query = sql.SQL("SELECT count(*) FROM {}").format(_qualified(table.name))
                row = pg.execute(query).fetchone()
                if row:
                    table.row_count = row[0]
                    continue
            except psycopg.errors.QueryCanceled:
                pass
            finally:
                pg.execute("ROLLBACK TO SAVEPOINT count")
        table.row_count_estimated = estimate is not None


def list_tables(data_store_id: uuid.UUID) -> tuple[list[Table], bool]:
    """The store's tables, by name, with their columns and row counts; and whether some were
    left out, to keep the list within `query_data`'s byte cap."""
    max_bytes = get_settings().query_data_max_bytes
    with _reading(data_store_id) as pg:
        tables = list(_catalog(pg).values())
        # As many whole tables as fit, in order. Sized before they're counted, so only those
        # shown are; a count's digits can't take one past the room left for them.
        shown: list[Table] = []
        size = 2  # the list's brackets
        for table in tables:
            size += len(json.dumps(table.listed(), ensure_ascii=False).encode()) + COUNT_ROOM
            if size > max_bytes:
                break
            shown.append(table)
        _count(pg, shown)
    return shown, len(shown) < len(tables)


def _text(column: Column) -> sql.Composable:
    """The column's value as text, as the page shows it: an embedding as its type and dimension
    (e.g. `vector(1024)`), never its numbers, and an array of them as how many (`3 × vector`);
    `jsonb` pretty-printed."""
    value = sql.SQL("{}.{}").format(SOURCE, sql.Identifier(column.name))
    if column.embedding and column.in_array:
        return sql.SQL("cardinality({}) || ' × ' || {}").format(
            value, sql.Literal(column.base_type)
        )
    if column.in_array:
        return sql.SQL("{}::text").format(value)
    if column.base_type == "sparsevec":
        # Written `{index:value,...}/dimension`.
        dims = sql.SQL("split_part({}::text, '/', 2)").format(value)
    elif column.embedding:
        dims = sql.SQL("vector_dims({})").format(value)
    elif column.base_type == "jsonb":
        return sql.SQL("jsonb_pretty({})").format(value)
    else:
        return sql.SQL("{}::text").format(value)
    # NULL stays NULL.
    return sql.SQL("{} || '(' || {} || ')'").format(sql.Literal(column.base_type), dims)


def read_rows(
    data_store_id: uuid.UUID,
    table_name: str,
    *,
    offset: int,
    limit: int,
    sort: str | None = None,
    descending: bool = False,
) -> RowsPage:
    """A page of the table's rows, from `offset`: at most `limit` of them, fewer if they'd go
    past `query_data`'s byte cap. In `sort`'s order (a column's name) if given, else as stored.

    Raises `TableNotFound` or `ColumnNotFound` for a name the catalog doesn't have."""
    max_bytes = get_settings().query_data_max_bytes
    with _reading(data_store_id) as pg:
        table = _table(pg, table_name)
        order = []
        if sort is not None:
            direction = sql.SQL("DESC" if descending else "ASC")
            column = sql.Identifier(table.column(sort).name)
            order.append(sql.SQL("{}.{} {} NULLS LAST").format(SOURCE, column, direction))
        # Then as stored, so rows that sort alike keep one order from page to page.
        order.append(sql.SQL("{0}.tableoid, {0}.ctid").format(SOURCE))
        cells = [sql.Identifier(f"c{i}") for i in range(len(table.columns))]
        # Short enough for a row of them to fit the byte cap, as JSON, if it can: none takes
        # more than 6 bytes a character.
        preview = sql.Literal(
            max(MIN_PREVIEW_CHARS, min(PREVIEW_CHARS, max_bytes // (6 * max(1, len(cells)))))
        )
        # The page's rows first (the subquery, which its LIMIT keeps whole), then each value cut
        # to its preview, and which were cut.
        statement = sql.SQL(
            "SELECT {outer} FROM ("
            "SELECT {inner} FROM {table} AS {source}"
            " ORDER BY {order} OFFSET {offset} LIMIT {limit}) page"
        ).format(
            outer=sql.SQL(", ").join(
                [
                    sql.SQL("key"),
                    *(sql.SQL("left({}, {})").format(c, preview) for c in cells),
                    sql.SQL("array_positions(ARRAY[{}]::boolean[], true)").format(
                        sql.SQL(", ").join(
                            sql.SQL("length({}) > {}").format(c, preview) for c in cells
                        )
                    ),
                ]
            ),
            inner=sql.SQL(", ").join(
                [
                    sql.SQL("{0}.tableoid::text || ':' || {0}.ctid::text AS key").format(SOURCE),
                    *(
                        sql.SQL("{} AS {}").format(_text(column), cell)
                        for column, cell in zip(table.columns, cells, strict=True)
                    ),
                ]
            ),
            table=_qualified(table.name),
            source=SOURCE,
            order=sql.SQL(", ").join(order),
            offset=sql.Literal(offset),
            # One more than the page, to tell whether there are more.
            limit=sql.Literal(limit + 1),
        )
        try:
            read = read_capped(
                pg,
                statement.as_string(pg),
                max_rows=limit,
                max_bytes=max_bytes,
                ensure_ascii=False,
            )
        except psycopg.errors.UndefinedFunction as exc:
            # Its type has no order (e.g. `point`).
            if sort is None:
                raise
            raise CantSort(sort) from exc
    if read.truncated and not read.rows:
        # Only the byte cap stops a read before its first row.
        raise RowTooWide(table_name)
    return RowsPage(
        columns=[c.name for c in table.columns],
        rows=[
            # Postgres counts the cut values from 1.
            Row(key=r[0], values=r[1:-1], truncated=[i - 1 for i in r[-1]])
            for r in read.rows
        ],
        has_more=read.truncated,
    )


def _row_key(key: str) -> tuple[int, str]:
    match = _ROW_KEY.fullmatch(key)
    # Oids and block numbers are 32 bits, places in a block 16.
    if match is None or max(int(match["table"]), int(match["block"])) >= 2**32:
        raise BadRowKey(key)
    if int(match["place"]) >= 2**16:
        raise BadRowKey(key)
    return int(match["table"]), f"({match['block']},{match['place']})"


def read_value(
    data_store_id: uuid.UUID, table_name: str, row_key: str, column_name: str
) -> tuple[str | None, bool]:
    """One value of a row (`Row.key`), in full as far as `query_data`'s byte cap allows; and
    whether it was cut short there.

    Raises `TableNotFound` or `ColumnNotFound` for a name the catalog doesn't have, `BadRowKey`
    for a key that isn't one, and `RowNotFound` if the row isn't there (any more)."""
    max_bytes = get_settings().query_data_max_bytes
    table_oid, tid = _row_key(row_key)
    # Characters that, as JSON, fit the cap however they're written: none takes more than 6
    # bytes (`\u001f`), and the brackets, quotes and flag take 16.
    chars = sql.Literal(max(1, (max_bytes - 16) // 6))
    with _reading(data_store_id) as pg:
        table = _table(pg, table_name)
        text = _text(table.column(column_name))
        statement = sql.SQL(
            "SELECT left({text}, {chars}), length({text}) > {chars} FROM {table} AS {source}"
            " WHERE {source}.tableoid = {oid}::oid AND {source}.ctid = {tid}::tid"
        ).format(
            text=text,
            chars=chars,
            table=_qualified(table.name),
            source=SOURCE,
            oid=sql.Literal(str(table_oid)),
            tid=sql.Literal(tid),
        )
        read = read_capped(
            pg, statement.as_string(pg), max_rows=1, max_bytes=max_bytes, ensure_ascii=False
        )
    if not read.rows:
        raise RowNotFound(row_key)
    value, cut = read.rows[0]
    return value, bool(cut)
