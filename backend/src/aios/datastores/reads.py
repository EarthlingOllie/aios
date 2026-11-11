"""Reading a data store as its read-only role, with capped results.

`query_data` (`aios.tools.data`) and the Data page's Tables (`aios.datastores.tables`) read
through here, so both get the same guards:

- `read_only` opens a `READ ONLY` transaction on the store's own database, as its `ro` role,
  under the role's statement timeout (`DATA_STORE_STATEMENT_TIMEOUT`). It's always rolled
  back, any advisory lock a statement took is let go first, and the connection is reset
  (`DISCARD ALL`) before it goes back to the pool, so nothing a statement set (settings,
  prepared statements, advisory locks) carries over, through the pooler or not.
- `read_capped` sends one statement with the extended query protocol, which takes exactly one,
  streams its rows, and stops at a number of rows or bytes, saying the result was `truncated`.
  A row is refused before the value that takes it past the byte cap is loaded.
"""

import json
import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import adapt, pq
from psycopg.types.json import set_json_loads
from psycopg.types.string import TextLoader

from aios.datastores.connections import store_session

# Types read as Postgres writes them, not as Python values: Python's can't hold some legal ones
# (infinities, years past 9999 or BC, 24:00, huge intervals), and one such value would fail the
# whole read. Also in arrays, whose loaders look these up.
TEXT_TYPES = (
    "date",
    "time",
    "timetz",
    "timestamp",
    "timestamptz",
    "interval",
    "daterange",
    "tsrange",
    "tstzrange",
    "datemultirange",
    "tsmultirange",
    "tstzmultirange",
)


@contextmanager
def read_only(data_store_id: uuid.UUID) -> Iterator[psycopg.Connection]:
    """A connection on the store's database, as its `ro` role, in a `READ ONLY` transaction
    that's rolled back on exit. Raises as `store_session` does."""
    with store_session(data_store_id, "ro") as session:
        # Autocommit at the driver, so the transaction below is the only one, and is ours.
        conn = session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
        # Read-only connections never prepare statements, which `DISCARD ALL` below would drop
        # (see `datastores.connections`).
        pg: psycopg.Connection = conn.connection.driver_connection
        try:
            pg.execute("BEGIN READ ONLY")
            # Back to here before the transaction ends, even if a statement failed it, to let go
            # of any session advisory lock a statement took while still on the same server
            # connection: through the pooler, `DISCARD ALL` after the transaction may run on
            # another (and nothing it would reset otherwise outlives a rolled-back transaction).
            pg.execute("SAVEPOINT reading")
            yield pg
        finally:
            try:
                pg.execute("ROLLBACK TO SAVEPOINT reading")
                pg.execute("SELECT pg_advisory_unlock_all()")
                pg.execute("ROLLBACK")
                pg.execute("DISCARD ALL")
            except psycopg.Error:
                # Never pool a connection in a state we don't know.
                conn.invalidate()


def _exact_float(literal: str) -> float | str:
    """A number in a JSON value, as a float if one holds it exactly, else as its text."""
    number = float(literal)
    if math.isfinite(number) and Decimal(repr(number)) == Decimal(literal):
        return number
    return literal


def _json_loads(data: str | bytes) -> Any:
    return json.loads(data, parse_float=_exact_float)


def jsonable(value: Any) -> Any:
    """A value as JSON, losing no precision (numerics as text)."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    # Numerics, UUIDs, ranges, and types psycopg doesn't know (e.g. `vector`), as text.
    return str(value)


def statement_columns(pg: psycopg.Connection, sql: str) -> tuple[list[str], list[int]]:
    """The names and type OIDs of the columns the statement returns, from parsing it (as one
    statement: the extended protocol takes no more) without running it."""
    pgconn = pg.pgconn
    for res in (pgconn.prepare(b"", sql.encode()), pgconn.describe_prepared(b"")):
        if res.status != pq.ExecStatus.COMMAND_OK:
            raise psycopg.errors.error_from_result(res, encoding=pg.info.encoding)
    names = [(res.fname(i) or b"").decode(pg.info.encoding) for i in range(res.nfields)]
    return names, [res.ftype(i) for i in range(res.nfields)]


class _RowTooBig(Exception):
    pass


class _RowBudget:
    """The bytes of text the row being loaded has taken so far."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.used = 0

    def take(self, size: int) -> None:
        self.used += size
        if self.used > self.max_bytes:
            raise _RowTooBig


def _cap_values(cur: psycopg.Cursor, types: list[int], budget: _RowBudget) -> None:
    """Makes `cur` refuse a row whose text is over the budget before loading the value that
    takes it over, as it can't fit the result anyway: a row is unbounded (one huge value, or
    many big ones), and loading a huge one (then dumping it as JSON) would take several times
    its size in memory."""
    for oid in set(types):
        # As psycopg loads it otherwise; types it doesn't know (e.g. `vector`), as text.
        loader = cur.adapters.get_loader(oid, pq.Format.TEXT) or cur.adapters.get_loader(
            0, pq.Format.TEXT
        )

        class Capped(adapt.Loader):
            def __init__(self, oid: int, context: Any = None, loader: Any = loader):
                super().__init__(oid, context)
                self._loader = loader(oid, context)

            def load(self, data: Any) -> Any:
                budget.take(len(data))
                return self._loader.load(data)

        cur.adapters.register_loader(oid, Capped)


@dataclass
class CappedRows:
    columns: list[str]
    # Each a list of values, as JSON (`jsonable`), in column order.
    rows: list[list]
    # Stopped at the row or byte cap, leaving the rest out.
    truncated: bool


def read_capped(
    pg: psycopg.Connection,
    sql: str,
    *,
    max_rows: int,
    max_bytes: int,
    columns: tuple[list[str], list[int]] | None = None,
    ensure_ascii: bool = True,
) -> CappedRows:
    """Runs the statement in the open transaction, reading its rows until `max_rows` of them,
    or `max_bytes` of them as JSON (escaped to ASCII as `json.dumps` does, unless
    `ensure_ascii` is false), whichever comes first. `columns` is the statement's, if already
    known (`statement_columns`)."""
    names, types = columns or statement_columns(pg, sql)
    cur = pg.cursor()
    for name in TEXT_TYPES:
        cur.adapters.register_loader(name, TextLoader)
    set_json_loads(_json_loads, cur)
    # After the text loaders, so the cap wraps them.
    budget = _RowBudget(max_bytes)
    _cap_values(cur, types, budget)
    rows: list[list] = []
    size, truncated = 0, False
    # `stream` sends it with the extended protocol too, and reads it a row at a time; leaving
    # early cancels the rest.
    try:
        for row in cur.stream(sql):
            budget.used = 0  # this row is loaded: the next starts afresh
            value = [jsonable(v) for v in row]
            row_size = len(json.dumps(value, ensure_ascii=ensure_ascii).encode()) + 1
            if len(rows) == max_rows or size + row_size > max_bytes:
                truncated = True
                break
            rows.append(value)
            size += row_size
    except _RowTooBig:
        truncated = True
        # libpq keeps the buffer it read the value into for as long as the connection is open,
        # so it's closed rather than pooled (`read_only` throws away one it can't roll back).
        pg.close()
    return CappedRows(columns=names, rows=rows, truncated=truncated)
