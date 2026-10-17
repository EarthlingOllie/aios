"""The agent tool that writes to the run's data store: `write_data`, the only way agents write.

The model never sends SQL. It names a table in `data`, an operation (insert, upsert, update or
delete), and the rows, values and filters as JSON; the platform checks each table and column
against the catalog and each value against its column's type, then builds the statements
itself, with quoted identifiers and bound values. It runs as the store's read-write role (`rw`),
in one transaction per call: a call is written whole, or not at all.

- Updates and deletes must say which rows they touch, by equality on columns (the primary key,
  or any others): there's no "every row".
- A call writes at most `WRITE_DATA_MAX_ROWS` rows. Inserts and upserts past it are refused
  before anything is run, as are updates and deletes that match more (they're counted first).
- Only `data` tables: the platform's (`aios`) are never written here.

Embedding columns are marked in `aios.embedding_columns`: a `vector(EMBEDDING_DIM)` column, and
the text column in the same table it embeds. Writing the text writes its embedding too, made
with the platform's embedding model; the model never sends vectors, and can't write those
columns itself. The embedding is a call to the provider, too slow to make holding a connection,
so a call that needs one is planned, embedded, then planned again and written; if the texts to
embed have changed in between (the table did), the call is refused.
"""

import logging
import math
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

import psycopg
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from aios.agent import ChatDeps
from aios.config import get_settings
from aios.datastores import store_session
from aios.embeddings import embed_documents
from aios.models import EMBEDDING_DIM
from aios.tools.data import DATA_NOT_INSTRUCTIONS, in_store, in_thread, jsonable, pg_error
from aios.tools.registry import AgentTool, ToolSpec, register

log = logging.getLogger(__name__)

SCHEMA = "data"

Operation = Literal["insert", "upsert", "update", "delete"]

# A table's columns as the catalog has them, in order, with which are its primary key and
# which are marked as embeddings (of which column).
COLUMNS_SQL = """
    SELECT a.attname, format_type(a.atttypid, a.atttypmod), t.typname, t.typcategory,
           a.attidentity, a.attgenerated,
           array_position(pk.indkey::int2[], a.attnum) AS pk_position,
           e.source_column
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    JOIN pg_type t ON t.oid = a.atttypid
    LEFT JOIN pg_index pk ON pk.indrelid = c.oid AND pk.indisprimary
    LEFT JOIN aios.embedding_columns e
      ON e.table_name = c.relname AND e.column_name = a.attname
    WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
    ORDER BY a.attnum
"""

TABLES_SQL = """
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
    ORDER BY c.relname
"""

# Column types by their catalog name, as the JSON the model must send for them.
TEXT_TYPES = {"text", "varchar", "bpchar", "citext", "name"}
INTEGER_RANGES = {"int2": 2**15, "int4": 2**31, "int8": 2**63}
FLOAT_TYPES = {"float4", "float8"}
# Postgres reads these from their text form: the model sends a string, and Postgres says if
# it's not one.
STRING_TYPES = {"date", "time", "timetz", "timestamp", "timestamptz", "interval", "inet", "cidr"}


class WriteDataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Refused(Exception):
    """A call the model can fix: unknown names, mistyped values, a missing filter..."""


@dataclass
class Column:
    name: str
    type: str  # as Postgres writes it, e.g. `numeric` or `vector(1024)`
    base: str  # the type's name in the catalog, e.g. `numeric`, `_text`, `vector`
    category: str  # `pg_type.typcategory`: `A` for arrays
    identity: str  # `a`: generated always (the database's alone); `d`: by default
    generated: str  # `s`: computed from other columns
    pk_position: int | None
    embedding_of: str | None


@dataclass
class Table:
    name: str
    columns: dict[str, Column]

    @property
    def qualified(self) -> str:
        return f"{SCHEMA}.{self.name}"

    @property
    def primary_key(self) -> list[str]:
        keyed = [c for c in self.columns.values() if c.pk_position is not None]
        return [c.name for c in sorted(keyed, key=lambda c: c.pk_position)]

    @property
    def embeddings(self) -> dict[str, list[str]]:
        """Each source text column, and the embedding columns made from it."""
        made: dict[str, list[str]] = {}
        for c in self.columns.values():
            if c.embedding_of is not None:
                made.setdefault(c.embedding_of, []).append(c.name)
        return made


@dataclass
class Statement:
    """One statement of the call, before it's SQL: the column values to write (`values`, with
    embeddings to fill as `None` until they're made) and the equality filters (`where`)."""

    values: dict[str, Any] = field(default_factory=dict)
    where: dict[str, Any] = field(default_factory=dict)
    # Embedding column -> the index in the plan's texts of the text it's made from.
    embed: dict[str, int] = field(default_factory=dict)


@dataclass
class Plan:
    table: Table
    operation: Operation
    statements: list[Statement]
    conflict: list[str]
    # The texts to embed, in order, for `Statement.embed`.
    texts: list[str]


@dataclass
class Call:
    table: str
    operation: Operation
    rows: list[dict[str, Any]] | None
    values: dict[str, Any] | None
    where: dict[str, Any] | None
    on_conflict: list[str] | None


def _table(pg: psycopg.Connection, name: str) -> Table:
    schema, dot, table = name.strip().rpartition(".")
    if dot and schema != SCHEMA:
        raise Refused(
            f"Only tables in the `{SCHEMA}` schema can be written, not `{name}`: the other"
            " schemas are the platform's."
        )
    rows = pg.execute(COLUMNS_SQL, (SCHEMA, table)).fetchall()
    if not rows:
        tables = [r[0] for r in pg.execute(TABLES_SQL, (SCHEMA,))]
        listed = ", ".join(tables) if tables else "none yet"
        raise Refused(
            f"There's no table `{table}` in `{SCHEMA}` (its tables: {listed}). Call"
            " `describe_data` to see them."
        )
    return Table(table, {r[0]: Column(*r) for r in rows})


def _column(table: Table, name: Any, where: str) -> Column:
    column = table.columns.get(name) if isinstance(name, str) else None
    if column is None:
        raise Refused(
            f"{where}: `{table.qualified}` has no column `{name}`. Its columns: "
            + ", ".join(table.columns)
            + "."
        )
    return column


def _writable(table: Table, name: Any, where: str) -> Column:
    column = _column(table, name, where)
    if column.embedding_of is not None:
        raise Refused(
            f"{where}: `{column.name}` is the embedding of `{column.embedding_of}`, made by the"
            f" platform when `{column.embedding_of}` is written. Leave it out."
        )
    if column.identity == "a" or column.generated:
        raise Refused(f"{where}: `{column.name}` is set by the database. Leave it out.")
    return column


def _value(column: Column, value: Any, where: str) -> Any:
    """The value to bind for `column`, checked against its type."""
    if value is None:
        return None

    def wrong(expected: str) -> Refused:
        return Refused(
            f"{where}: `{column.name}` is {column.type}, so it takes {expected}; got"
            f" {type(value).__name__} {value!r}."
        )

    base, is_number = column.base, isinstance(value, (int, float)) and not isinstance(value, bool)
    if base in ("json", "jsonb"):
        return Jsonb(value)
    if column.category == "A":
        if not isinstance(value, list):
            raise wrong("a list")
        return value
    if base in TEXT_TYPES:
        if not isinstance(value, str):
            raise wrong("a string")
        return value
    if base in INTEGER_RANGES:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        bound = INTEGER_RANGES[base]
        if not (isinstance(value, int) and not isinstance(value, bool)):
            raise wrong("a whole number")
        if not -bound <= value < bound:
            raise wrong(f"a whole number from {-bound} to {bound - 1}")
        return value
    if base == "numeric" or base in FLOAT_TYPES:
        if isinstance(value, float) and not math.isfinite(value):
            raise wrong("a finite number")
        if is_number:
            return value
        if isinstance(value, str):
            try:
                Decimal(value)
            except InvalidOperation:
                raise wrong("a number") from None
            return value  # its text: no precision lost on the way
        raise wrong("a number")
    if base == "bool":
        if not isinstance(value, bool):
            raise wrong("true or false")
        return value
    if base == "uuid":
        try:
            if isinstance(value, str):
                return uuid.UUID(value)
        except ValueError:
            pass
        raise wrong("a UUID string")
    if base == "vector":
        if not (
            isinstance(value, list)
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        ):
            raise wrong("a list of numbers")
        return "[" + ",".join(str(float(v)) for v in value) + "]"
    if base in STRING_TYPES:
        if not isinstance(value, str):
            raise wrong("a string")
        return value
    # Anything else Postgres reads from text, and says if it can't.
    if isinstance(value, (dict, list)):
        raise wrong("a string")
    return value if isinstance(value, str) else str(value)


def _dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise Refused(f"`{where}` must be an object of column names to values, and not empty.")
    return value


def _filters(table: Table, where: Any) -> dict[str, Any]:
    if not where:
        raise Refused(
            "Name the rows to change with `where`: column names to the values they must equal"
            + (
                f" (e.g. its primary key, {', '.join(table.primary_key)})"
                if table.primary_key
                else ""
            )
            + ". Every row at once isn't allowed."
        )
    filters = _dict(where, "where")
    out = {}
    for name, value in filters.items():
        column = _column(table, name, "where")
        if column.embedding_of is not None:
            raise Refused(f"where: `{name}` is an embedding, and can't be filtered on.")
        out[name] = _value(column, value, "where")
    return out


def _plan(pg: psycopg.Connection, call: Call) -> Plan:
    """The call checked against the catalog, as statements to run."""
    max_rows = get_settings().write_data_max_rows
    table = _table(pg, call.table)
    embeddings = table.embeddings
    for source, made in embeddings.items():
        if source not in table.columns:
            raise Refused(
                f"`{table.qualified}`'s embedding column `{made[0]}` is of `{source}`, which it"
                " doesn't have. The table needs a schema change before it can be written."
            )
        for name in made:
            if table.columns[name].base != "vector":
                raise Refused(
                    f"`{table.qualified}.{name}` is marked as an embedding but isn't a vector."
                    " The table needs a schema change before it can be written."
                )
    texts: list[str] = []

    def values(row: dict[str, Any], where: str) -> Statement:
        statement = Statement()
        for name, value in row.items():
            statement.values[name] = _value(_writable(table, name, where), value, where)
            for embedding in embeddings.get(name, []):
                # Blank text has nothing to embed: its embedding is cleared.
                if isinstance(value, str) and value.strip():
                    statement.embed[embedding] = len(texts)
                    texts.append(value)
                statement.values[embedding] = None
        return statement

    conflict: list[str] = []
    if call.operation in ("insert", "upsert"):
        if call.values is not None or call.where is not None:
            raise Refused(f"An {call.operation} takes `rows`, not `values` or `where`.")
        if not isinstance(call.rows, list) or not call.rows:
            raise Refused(f"An {call.operation} needs `rows`: a list of objects, one per row.")
        if len(call.rows) > max_rows:
            raise Refused(
                f"That's {len(call.rows)} rows; one call writes at most {max_rows}. Split them"
                " across calls."
            )
        statements = [
            values(_dict(row, f"rows[{i}]"), f"rows[{i}]") for i, row in enumerate(call.rows)
        ]
        if call.operation == "upsert":
            conflict = call.on_conflict or table.primary_key
            if not conflict:
                raise Refused(
                    f"`{table.qualified}` has no primary key: name the columns that identify a"
                    " row in `on_conflict` (they need a unique constraint)."
                )
            for name in conflict:
                _column(table, name, "on_conflict")
            for i, statement in enumerate(statements):
                missing = [name for name in conflict if name not in statement.values]
                if missing:
                    raise Refused(
                        f"rows[{i}] lacks {', '.join(missing)}: an upsert finds the row to"
                        f" update by {', '.join(conflict)}"
                        + (
                            ", which the database sets, so give `on_conflict` columns with a"
                            " unique constraint instead"
                            if any(table.columns[n].identity == "a" for n in missing)
                            else ""
                        )
                        + "."
                    )
        elif call.on_conflict is not None:
            raise Refused("Only an upsert takes `on_conflict`.")
    elif call.operation == "update":
        if call.rows is not None or call.on_conflict is not None:
            raise Refused("An update takes `values` and `where`, not `rows` or `on_conflict`.")
        statement = values(_dict(call.values, "values"), "values")
        statement.where = _filters(table, call.where)
        statements = [statement]
    elif call.operation == "delete":
        if call.rows is not None or call.values is not None or call.on_conflict is not None:
            raise Refused("A delete takes only `where`.")
        statements = [Statement(where=_filters(table, call.where))]
    else:
        raise Refused(f"Unknown operation {call.operation!r}: insert, upsert, update or delete.")
    return Plan(table, call.operation, statements, conflict, texts)


def _ident(pg: psycopg.Connection, *names: str) -> sql.SQL:
    """A quoted name, with any `%` in it doubled: psycopg reads `%` as the start of a
    placeholder even inside quotes."""
    return sql.SQL(sql.Identifier(*names).as_string(pg).replace("%", "%%"))


def _where(pg: psycopg.Connection, statement: Statement) -> sql.Composed:
    return sql.SQL(" AND ").join(
        sql.SQL("{} IS NULL").format(_ident(pg, name))
        if value is None
        else sql.SQL("{} = {}").format(_ident(pg, name), sql.Placeholder(f"w{i}"))
        for i, (name, value) in enumerate(statement.where.items())
    )


def _count_sql(pg: psycopg.Connection, plan: Plan, statement: Statement, at_most: int):
    """How many rows the update or delete matches, counting no further than `at_most`."""
    return sql.SQL("SELECT count(*) FROM (SELECT FROM {} WHERE {} LIMIT {}) AS matched").format(
        _ident(pg, SCHEMA, plan.table.name), _where(pg, statement), sql.Literal(at_most)
    )


def _sql(pg: psycopg.Connection, plan: Plan, statement: Statement) -> sql.Composed:
    def ident(*names: str) -> sql.SQL:
        return _ident(pg, *names)

    table = ident(SCHEMA, plan.table.name)
    cols = [ident(name) for name in statement.values]
    params = [sql.Placeholder(f"v{i}") for i in range(len(statement.values))]
    filters = _where(pg, statement)
    pk = plan.table.primary_key
    returning = (
        sql.SQL(" RETURNING {}").format(sql.SQL(", ").join(map(ident, pk))) if pk else sql.SQL("")
    )
    if plan.operation in ("insert", "upsert"):
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            table, sql.SQL(", ").join(cols), sql.SQL(", ").join(params)
        )
        if plan.operation == "upsert":
            # Every column given, the conflict's too (a no-op), so the row always comes back.
            query += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                sql.SQL(", ").join(map(ident, plan.conflict)),
                sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(c) for c in cols),
            )
    elif plan.operation == "update":
        query = sql.SQL("UPDATE {} SET {} WHERE {}").format(
            table,
            sql.SQL(", ").join(
                sql.SQL("{} = {}").format(c, p) for c, p in zip(cols, params, strict=True)
            ),
            filters,
        )
    else:
        query = sql.SQL("DELETE FROM {} WHERE {}").format(table, filters)
    return query + returning


def _params(statement: Statement, vectors: list[list[float]]) -> dict[str, Any]:
    """The statement's values (embeddings from `vectors`) and filters, by their placeholders'
    names (`_sql`): by position, as column names needn't be valid placeholder names."""
    values = dict(statement.values)
    for column, index in statement.embed.items():
        values[column] = "[" + ",".join(map(str, vectors[index])) + "]"
    params = {f"v{i}": value for i, value in enumerate(values.values())}
    params.update({f"w{i}": value for i, value in enumerate(statement.where.values())})
    return params


@dataclass
class Embedded:
    """The texts a call embeds, and their embeddings, in order."""

    texts: list[str]
    vectors: list[list[float]]


@dataclass
class NeedsEmbedding:
    """What `_apply` gives, writing nothing, when the call has texts to embed that `Embedded`
    doesn't have."""

    texts: list[str]


def _execute(cur: psycopg.Cursor, query: sql.Composed, params: dict, where: str) -> None:
    """Runs one of the call's statements; a failure is the model's to fix (a constraint, a
    value Postgres can't read...)."""
    try:
        cur.execute(query, params)
    except psycopg.Error as exc:
        detail = exc.diag.message_detail
        raise Refused(where + pg_error(exc) + (f" {detail}" if detail else "")) from exc


def _over_cap(plan: Plan, max_rows: int) -> Refused:
    return Refused(
        f"That {plan.operation} matches more than {max_rows} row{'' if max_rows == 1 else 's'};"
        f" one call changes at most {max_rows}. Nothing was changed: narrow `where`."
    )


def _apply(data_store_id: uuid.UUID, call: Call, embedded: Embedded | None) -> Any:
    """Writes the call, or says what it needs embedding first (`NeedsEmbedding`)."""
    max_rows = get_settings().write_data_max_rows
    with store_session(data_store_id, "rw") as session:
        pg: psycopg.Connection = session.connection().connection.driver_connection
        try:
            plan = _plan(pg, call)
            vectors: list[list[float]] = []
            if plan.texts:
                if embedded is None or embedded.texts != plan.texts:
                    return NeedsEmbedding(plan.texts)
                vectors = embedded.vectors
            written: list[dict] = []
            count = 0
            with pg.cursor() as cur:
                for i, statement in enumerate(plan.statements):
                    params = _params(statement, vectors)
                    where = f"rows[{i}]: " if plan.operation in ("insert", "upsert") else ""
                    if plan.operation in ("update", "delete"):
                        # Counted first, so one that matches too many is never run: it would
                        # lock every row it matched, and return them all.
                        _execute(cur, _count_sql(pg, plan, statement, max_rows + 1), params, "")
                        if cur.fetchone()[0] > max_rows:
                            raise _over_cap(plan, max_rows)
                    _execute(cur, _sql(pg, plan, statement), params, where)
                    count += cur.rowcount
                    # Rows can match in between, as others write.
                    if count > max_rows:
                        raise _over_cap(plan, max_rows)
                    if cur.description is not None:
                        names = [d.name for d in cur.description]
                        written += [
                            dict(zip(names, map(jsonable, row), strict=True))
                            for row in cur.fetchall()
                        ]
            session.commit()
        except Refused as exc:
            return {"error": str(exc)}
    result: dict = {"table": plan.table.qualified, "operation": plan.operation, "row_count": count}
    if plan.table.primary_key:
        result["rows"] = written
    return result


async def _embed(texts: list[str]) -> Embedded | dict:
    try:
        vectors = await embed_documents(texts)
    except Exception:
        log.exception("write_data couldn't embed %d texts", len(texts))
        return {
            "error": "The text couldn't be embedded just now, so nothing was written. Try"
            " again in a moment."
        }
    if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIM for v in vectors):
        log.error("The embedding model gave the wrong number or size of vectors")
        return {"error": "The text couldn't be embedded, so nothing was written."}
    return Embedded(texts, vectors)


async def _write(data_store_id: uuid.UUID, call: Call) -> dict:
    result = await in_thread(data_store_id, _apply, call, None)
    if not isinstance(result, NeedsEmbedding):
        return result
    embedded = await _embed(result.texts)
    if isinstance(embedded, dict):
        return embedded
    result = await in_thread(data_store_id, _apply, call, embedded)
    if isinstance(result, NeedsEmbedding):
        return {"error": "The table changed while writing, and nothing was written. Try again."}
    return result


def _description() -> str:
    max_rows = get_settings().write_data_max_rows
    return f"""Write rows to a table in the user's data store: insert, upsert, update or
delete. This is the only way to change the data in it; `query_data` reads it.

- `table`: a table in the `{SCHEMA}` schema (e.g. `contacts` or `{SCHEMA}.contacts`). Call
  `describe_data` for the tables and their columns. Tables in other schemas can't be written,
  and tables and columns are changed only by schema changes.
- `operation`:
  - `insert`: `rows`, a list of objects of column names to values.
  - `upsert`: `rows`, each inserted, or updating the row it conflicts with on `on_conflict`
    (columns with a unique constraint; the primary key if left out).
  - `update`: `values` (column names to new values) on the rows matching `where`.
  - `delete`: the rows matching `where`.
- `where`: column names to the values the rows' columns must equal (all of them; `null`
  matches null). Updates and deletes must have one: name rows by primary key where you can.
- Values are JSON matching each column's type: strings for text, dates and times (ISO 8601),
  numbers for numbers, true or false, any JSON for json columns. Leave out columns the database
  sets (identities, generated columns).
- Embedding columns (`embedding_of` in `describe_data`) are filled for you from their text
  column: never send them, nor filter on them.

At most {max_rows} rows a call, written all together or not at all. The result has
`row_count`, and `rows`: the primary keys of the rows written. On failure it has an `error`
instead, and nothing was written.

What you send is stored as data, exactly as given: it's never run as SQL. {DATA_NOT_INSTRUCTIONS}"""


def build(config: WriteDataConfig) -> list[AgentTool]:
    async def write_data(
        table: Annotated[str, "A table in the data schema."],
        operation: Annotated[Operation, "insert, upsert, update or delete."],
        runtime: ToolRuntime[ChatDeps],
        rows: Annotated[
            list[dict[str, Any]] | None, "insert and upsert: the rows, as column -> value."
        ] = None,
        values: Annotated[dict[str, Any] | None, "update: column -> new value."] = None,
        where: Annotated[
            dict[str, Any] | None, "update and delete: column -> the value rows must equal."
        ] = None,
        on_conflict: Annotated[
            list[str] | None, "upsert: the unique columns rows are matched on (default: the key)."
        ] = None,
    ) -> str:
        call = Call(table, operation, rows, values, where, on_conflict)
        return await in_store(runtime, _write, call)

    return [StructuredTool.from_function(coroutine=write_data, description=_description())]


register(
    ToolSpec(
        key="write_data",
        name="Write data",
        description="Insert, update and delete rows in your data store's tables.",
        config_model=WriteDataConfig,
        build=build,
    )
)
