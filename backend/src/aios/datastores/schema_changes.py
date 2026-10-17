"""Schema change requests: how an agent changes the tables in its store's `data` schema.

The model never sends SQL. It sends a spec (`SchemaChange`): a list of operations
(`create_table`, `add_column`, `create_index`, `drop_table`, `drop_column`), each naming
tables, columns and types from an allowlist. The spec is checked against the store's current
schema (`check`) and compiled to SQL (`compile_sql`) with every identifier quoted; that SQL is
what the owner approves, and exactly what runs. Only the `data` schema is touched, apart from
the platform's own bookkeeping: `aios.embedding_columns`, which marks embedding columns for
`write_data`, and `aios.dropped_objects`.

Drops are reversible: they rename the table or column to `_dropped_<timestamp>_<name>` and
record it in `aios.dropped_objects`, and applying one takes the store's login roles' access
to it away (`aios.datastores.dropped`), so the data tools no longer see it. The owner can
restore it until it's purged, `DATA_STORE_DROPPED_RETENTION_DAYS` later. Approving a request
that drops anything takes the names of the tables it drops from, typed (`confirmation`).

The flow (see `aios.tools.schema_change` and `aios.api.schema_changes`):

1. `propose` checks a spec against the store's schema (read as its read-only role), compiles
   it, and records a `SchemaChangeRequest` (platform database) at the store's current data
   schema revision (`read_revision`: the latest `aios.schema_history` entry).
2. The owner approves or rejects it (only users who pass `resolve.can_approve`).
3. `apply`, on the provisioning worker (only it has the provisioner's credentials), runs an
   approved request's SQL as the store's owner, in one transaction under a `lock_timeout`
   (`SCHEMA_CHANGE_LOCK_TIMEOUT`), and appends it to `aios.schema_history`, which bumps the
   revision. If the revision has moved on since the request was made, the spec is checked
   again first, and a request that no longer applies is marked `stale` instead.
4. The run that asked is resumed with the `outcome`.
"""

import copy
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Any, Literal

import psycopg
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import func, select, update

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.models import EMBEDDING_DIM
from aios.db import SessionLocal
from aios.models import DataStore, SchemaChangeRequest, SchemaChangeStatus

if TYPE_CHECKING:
    from aios.datastores.provision import StoreNames

log = logging.getLogger(__name__)

SCHEMA = "data"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Postgres's reserved words (`pg_get_keywords()`, categories R and T): legal when quoted, which
# the compiler always does, but the model writes `query_data`'s SQL unquoted.
RESERVED = frozenset(
    """all analyse analyze and any array as asc asymmetric authorization binary both case cast
    check collate collation column concurrently constraint create cross current_catalog
    current_date current_role current_schema current_time current_timestamp current_user
    default deferrable desc distinct do else end except false fetch for foreign freeze from
    full grant group having ilike in initially inner intersect into is isnull join lateral
    leading left like limit localtime localtimestamp natural not notnull null offset on only
    or order outer overlaps placing primary references returning right select session_user
    similar some symmetric system_user table tablesample then to trailing true union unique
    user using variadic verbose when where window with""".split()  # noqa: SIM905
)


def _name(value: str) -> str:
    if not NAME_PATTERN.match(value):
        raise ValueError(
            f"{value!r} isn't a valid name: lowercase letters, digits and underscores, starting"
            " with a letter, at most 63 characters"
        )
    if value in RESERVED:
        raise ValueError(f"{value!r} is a reserved word in SQL; pick another name")
    return value


def _text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text can't contain NUL characters")
    return value


Name = Annotated[str, AfterValidator(_name)]
Description = Annotated[str, Field(max_length=1000), AfterValidator(_text)]

ColumnType = Literal[
    "text",
    "integer",
    "bigint",
    "numeric",
    "boolean",
    "date",
    "timestamptz",
    "jsonb",
    "uuid",
    "vector",
    "embedding",
]

# The most dimensions an HNSW index takes (pgvector's limit).
HNSW_MAX_DIMENSIONS = 2000

# Each type as Postgres's `format_type` writes it, which is how a store's existing columns are
# read (`read_schema`), so new and existing columns compare alike.
_FORMAT = {
    "text": "text",
    "integer": "integer",
    "bigint": "bigint",
    "numeric": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamptz": "timestamp with time zone",
    "jsonb": "jsonb",
    "uuid": "uuid",
}


class Column(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    type: ColumnType
    # `vector` only: how many dimensions.
    dimensions: int | None = Field(None, ge=1, le=16000)
    # `embedding` only: the text column (in the same table) it's the embedding of.
    embedding_of: Name | None = None
    nullable: bool = True
    # A constant of the column's type; "now" for a date or timestamptz (the time of the
    # insert), "random" for a uuid (a new random one).
    default: str | int | float | bool | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def _fits_type(self) -> "Column":
        if (self.type == "vector") != (self.dimensions is not None):
            raise ValueError("`dimensions` is for vector columns, which need it")
        if (self.type == "embedding") != (self.embedding_of is not None):
            raise ValueError("`embedding_of` is for embedding columns, which need it")
        if self.type == "embedding" and not self.nullable:
            raise ValueError("an embedding column is nullable: it's empty while its text is")
        if self.default is not None:
            _default_sql(self)  # raises if it isn't one of the type's
        return self

    @property
    def pg_type(self) -> str:
        """The type as `format_type` writes it (without the `extensions` schema)."""
        if self.type == "vector":
            return f"vector({self.dimensions})"
        if self.type == "embedding":
            return f"vector({EMBEDDING_DIM})"
        return _FORMAT[self.type]


class CreateTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create_table"]
    name: Name
    description: Description | None = None
    columns: list[Column] = Field(min_length=1, max_length=100)
    # The columns of its primary key; left out, it gets a `bigint` identity column `id`.
    primary_key: list[Name] | None = Field(None, min_length=1, max_length=8)


class AddColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["add_column"]
    table: Name
    column: Column


class CreateIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create_index"]
    table: Name
    columns: list[Name] = Field(min_length=1, max_length=8)
    # `btree` for equality, ranges and sorting; `gin` for a jsonb column; `fulltext` for
    # searching a text column's words; `hnsw` for nearest neighbours of a vector or embedding.
    method: Literal["btree", "gin", "fulltext", "hnsw"] = "btree"
    unique: bool = False
    # Left out, one is made from the table and columns.
    name: Name | None = None


# What a dropped table or column is renamed to: `_dropped_<when it was asked for>_<its name>`
# (cut short to fit Postgres's 63 characters). Never a name the model can give anything else,
# which starts with a letter (`NAME_PATTERN`).
DROPPED_PREFIX = "_dropped_"
DROPPED_PATTERN = re.compile(r"^_dropped_\d{14}_[a-z0-9_]{1,39}$")


def _dropped_name(value: str | None) -> str | None:
    if value is not None and not DROPPED_PATTERN.match(value):
        raise ValueError(f"{value!r} isn't a name a dropped table or column is given")
    return value


# Filled in by `check`, never by the model (so not in the schema it's shown).
DroppedName = SkipJsonSchema[Annotated[str | None, AfterValidator(_dropped_name)]]
Found = SkipJsonSchema[bool]


class DropTable(BaseModel):
    """Renames the table out of the way (`dropped_as`) rather than dropping it: the owner can
    restore it until it's purged (`aios.datastores.dropped`)."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["drop_table"]
    table: Name
    dropped_as: DroppedName = None
    # What `check` found: whether embedding columns are marked in it, and whether it has
    # dropped columns (both follow it to its new name).
    has_embeddings: Found = False
    has_dropped_columns: Found = False


class DropColumn(BaseModel):
    """Renames the column out of the way (`dropped_as`), as `DropTable` does a table."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["drop_column"]
    table: Name
    column: Name
    dropped_as: DroppedName = None
    # What `check` found: whether it's NOT NULL (it stops being, so new rows can leave it
    # out), and whether it's an embedding column (its marker follows it).
    not_null: Found = False
    is_embedding: Found = False


Operation = Annotated[
    CreateTable | AddColumn | CreateIndex | DropTable | DropColumn, Field(discriminator="op")
]
Op = CreateTable | AddColumn | CreateIndex | DropTable | DropColumn


class SchemaChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ops: list[Operation] = Field(min_length=1, max_length=20)


class Invalid(ValueError):
    """A spec the store's schema doesn't take: the message says why, for the model."""


@dataclass
class StoreSchema:
    """What a spec is checked against: the `data` schema's tables, each column's type (as
    `format_type` writes it), and every name taken in the schema (tables, indexes,
    sequences...); each table's primary key and NOT NULL columns, the embedding columns (and
    the text column each is of), and each table's dropped columns. Dropped tables and columns
    aren't among `tables` or their columns, but a dropped table's name is still taken."""

    tables: dict[str, dict[str, str]] = field(default_factory=dict)
    relations: set[str] = field(default_factory=set)
    primary_keys: dict[str, list[str]] = field(default_factory=dict)
    not_null: dict[str, set[str]] = field(default_factory=dict)
    embeddings: dict[tuple[str, str], str] = field(default_factory=dict)
    dropped_columns: dict[str, set[str]] = field(default_factory=dict)


def _quote(name: str) -> str:
    """An identifier, quoted: safe whatever it holds."""
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    """A string constant (with `standard_conforming_strings`, Postgres's default)."""
    return "'" + value.replace("'", "''") + "'"


def _qualified(table: str) -> str:
    return f"{_quote(SCHEMA)}.{_quote(table)}"


def _type_sql(column: Column) -> str:
    if column.type in ("vector", "embedding"):
        dimensions = column.dimensions if column.type == "vector" else EMBEDDING_DIM
        # pgvector is in the store's `extensions` schema.
        return f'"extensions"."vector"({dimensions})'
    return column.type


def _default_sql(column: Column) -> str:
    value, kind = column.default, column.type
    wrong = Invalid(f"{value!r} isn't a default for the {kind} column `{column.name}`")
    if kind == "text" and isinstance(value, str):
        return _literal(_text(value))
    if kind in ("integer", "bigint") and isinstance(value, int) and not isinstance(value, bool):
        limit = 2**31 if kind == "integer" else 2**63
        if not -limit <= value < limit:
            raise wrong
        return str(value)
    if kind == "numeric" and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise wrong from None
        if not number.is_finite():
            raise wrong
        return str(number)
    if kind == "boolean" and isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if kind == "date" and isinstance(value, str):
        if value == "now":
            return "CURRENT_DATE"
        try:
            return _literal(date.fromisoformat(value).isoformat())
        except ValueError:
            raise wrong from None
    if kind == "timestamptz" and isinstance(value, str):
        if value == "now":
            return "now()"
        try:
            return _literal(datetime.fromisoformat(value).isoformat())
        except ValueError:
            raise wrong from None
    if kind == "uuid" and value == "random":
        return "gen_random_uuid()"
    raise wrong


def _column_sql(column: Column) -> str:
    sql = f"{_quote(column.name)} {_type_sql(column)}"
    if not column.nullable:
        sql += " NOT NULL"
    if column.default is not None:
        sql += f" DEFAULT {_default_sql(column)}"
    return sql


def _comment_sql(table: str, column: Column) -> list[str]:
    if column.description is None:
        return []
    return [
        f"COMMENT ON COLUMN {_qualified(table)}.{_quote(column.name)}"
        f" IS {_literal(column.description)};"
    ]


def _marker_sql(table: str, column: Column) -> list[str]:
    if column.type != "embedding":
        return []
    return [
        f"INSERT INTO {_quote('aios')}.{_quote('embedding_columns')}"
        " (table_name, column_name, source_column)"
        f" VALUES ({_literal(table)}, {_literal(column.name)}, {_literal(column.embedding_of)});"
    ]


def _create_table_sql(op: CreateTable) -> list[str]:
    lines = []
    if op.primary_key is None:
        lines.append(f"{_quote('id')} bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    lines += [_column_sql(c) for c in op.columns]
    if op.primary_key is not None:
        lines.append(f"PRIMARY KEY ({', '.join(map(_quote, op.primary_key))})")
    body = ",\n".join(f"    {line}" for line in lines)
    statements = [f"CREATE TABLE {_qualified(op.name)} (\n{body}\n);"]
    if op.description is not None:
        statements.append(f"COMMENT ON TABLE {_qualified(op.name)} IS {_literal(op.description)};")
    for column in op.columns:
        statements += _comment_sql(op.name, column)
    for column in op.columns:
        statements += _marker_sql(op.name, column)
    return statements


def _add_column_sql(op: AddColumn) -> list[str]:
    return [
        f"ALTER TABLE {_qualified(op.table)} ADD COLUMN {_column_sql(op.column)};",
        *_comment_sql(op.table, op.column),
        *_marker_sql(op.table, op.column),
    ]


def _create_index_sql(op: CreateIndex) -> list[str]:
    columns = ", ".join(map(_quote, op.columns))
    using = {
        "btree": f"btree ({columns})",
        "gin": f"gin ({columns})",
        "fulltext": f"gin (to_tsvector('english', {columns}))",
        "hnsw": f'hnsw ({columns} "extensions"."vector_cosine_ops")',
    }[op.method]
    unique = "UNIQUE " if op.unique else ""
    return [f"CREATE {unique}INDEX {_quote(op.name)} ON {_qualified(op.table)} USING {using};"]


_DROPPED_OBJECTS = f"{_quote('aios')}.{_quote('dropped_objects')}"
_EMBEDDING_COLUMNS = f"{_quote('aios')}.{_quote('embedding_columns')}"


def _record_dropped_sql(table: str, column: str | None, dropped_as: str, not_null: bool) -> str:
    return (
        f"INSERT INTO {_DROPPED_OBJECTS} (table_name, column_name, dropped_name, not_null)"
        f" VALUES ({_literal(table)}, {_literal(column) if column else 'NULL'},"
        f" {_literal(dropped_as)}, {'TRUE' if not_null else 'FALSE'});"
    )


def _drop_table_sql(op: DropTable) -> list[str]:
    new, old = _literal(op.dropped_as), _literal(op.table)
    statements = [f"ALTER TABLE {_qualified(op.table)} RENAME TO {_quote(op.dropped_as)};"]
    if op.has_embeddings:
        statements.append(
            f"UPDATE {_EMBEDDING_COLUMNS} SET table_name = {new} WHERE table_name = {old};"
        )
    if op.has_dropped_columns:
        statements.append(
            f"UPDATE {_DROPPED_OBJECTS} SET table_name = {new}"
            f" WHERE table_name = {old} AND column_name IS NOT NULL;"
        )
    statements.append(_record_dropped_sql(op.table, None, op.dropped_as, False))
    return statements


def _drop_column_sql(op: DropColumn) -> list[str]:
    table = _qualified(op.table)
    statements = [
        f"ALTER TABLE {table} RENAME COLUMN {_quote(op.column)} TO {_quote(op.dropped_as)};"
    ]
    if op.not_null:
        statements.append(
            f"ALTER TABLE {table} ALTER COLUMN {_quote(op.dropped_as)} DROP NOT NULL;"
        )
    if op.is_embedding:
        statements.append(
            f"UPDATE {_EMBEDDING_COLUMNS} SET column_name = {_literal(op.dropped_as)}"
            f" WHERE table_name = {_literal(op.table)} AND column_name = {_literal(op.column)};"
        )
    statements.append(_record_dropped_sql(op.table, op.column, op.dropped_as, op.not_null))
    return statements


def compile_sql(change: SchemaChange) -> str:
    """The SQL a checked spec (`check`) runs: what the owner approves."""
    statements: list[str] = []
    for op in change.ops:
        if isinstance(op, CreateTable):
            statements += _create_table_sql(op)
        elif isinstance(op, AddColumn):
            statements += _add_column_sql(op)
        elif isinstance(op, CreateIndex):
            statements += _create_index_sql(op)
        elif isinstance(op, DropTable):
            statements += _drop_table_sql(op)
        else:
            statements += _drop_column_sql(op)
    return "\n".join(statements)


def confirmation(change: SchemaChange) -> str | None:
    """What the owner types to approve a request that drops anything: the names of the tables
    it drops, or drops columns from, in order; None for one that drops nothing."""
    tables = [op.table for op in change.ops if isinstance(op, (DropTable, DropColumn))]
    return ", ".join(dict.fromkeys(tables)) or None


def check(change: SchemaChange, schema: StoreSchema, now: datetime | None = None) -> SchemaChange:
    """The spec, checked against `schema` op by op (each seeing the ones before it), with
    what it leaves to the platform filled in: index names it left out, the names drops rename
    to (from `now`, the time it's asked for, unless they have them already), and what drops
    found. Raises `Invalid` if the schema doesn't take it."""
    state = copy.deepcopy(schema)
    tables, relations = state.tables, state.relations
    now = now or datetime.now(UTC)
    checked: list[Op] = []
    for i, op in enumerate(change.ops, start=1):
        where = f"Operation {i} ({op.op})"
        if isinstance(op, CreateTable):
            _check_create_table(op, tables, relations, where)
            state.primary_keys[op.name] = op.primary_key or ["id"]
            state.not_null[op.name] = {c.name for c in op.columns if not c.nullable}
            state.not_null[op.name].update(state.primary_keys[op.name])
            for c in op.columns:
                if c.embedding_of:
                    state.embeddings[op.name, c.name] = c.embedding_of
        elif isinstance(op, AddColumn):
            _check_add_column(op, tables, where, existing=op.table in schema.tables)
            if not op.column.nullable:
                state.not_null.setdefault(op.table, set()).add(op.column.name)
            if op.column.embedding_of:
                state.embeddings[op.table, op.column.name] = op.column.embedding_of
        elif isinstance(op, CreateIndex):
            op = _check_create_index(op, tables, relations, where)
        elif isinstance(op, DropTable):
            op = _check_drop_table(op, state, now, where)
        else:
            op = _check_drop_column(op, state, now, where)
        checked.append(op)
    return SchemaChange(ops=checked)


def _dropped_as(op: DropTable | DropColumn, now: datetime, name: str, taken: set[str]) -> str:
    """The name it's renamed to: the one it has (checked again), or one from `now` and its name
    that isn't `taken` (a later second's, if it was dropped once already this second)."""
    if op.dropped_as:
        return op.dropped_as
    while True:
        stamp = f"{DROPPED_PREFIX}{now:%Y%m%d%H%M%S}_"
        dropped_as = stamp + name[: 63 - len(stamp)]
        if dropped_as not in taken:
            return dropped_as
        now += timedelta(seconds=1)


def _check_drop_table(op: DropTable, state: StoreSchema, now: datetime, where: str) -> DropTable:
    if op.table not in state.tables:
        raise Invalid(f"{where}: there's no table `{SCHEMA}.{op.table}`")
    dropped_as = _dropped_as(op, now, op.table, state.relations)
    if dropped_as in state.relations:
        raise Invalid(f"{where}: `{SCHEMA}.{dropped_as}` already exists")
    checked = op.model_copy(
        update={
            "dropped_as": dropped_as,
            "has_embeddings": any(t == op.table for t, _ in state.embeddings),
            "has_dropped_columns": bool(state.dropped_columns.get(op.table)),
        }
    )
    del state.tables[op.table]
    state.relations.discard(op.table)
    state.relations.add(dropped_as)
    state.embeddings = {k: v for k, v in state.embeddings.items() if k[0] != op.table}
    state.dropped_columns.pop(op.table, None)
    return checked


def _check_drop_column(op: DropColumn, state: StoreSchema, now: datetime, where: str) -> DropColumn:
    columns = state.tables.get(op.table)
    if columns is None:
        raise Invalid(f"{where}: there's no table `{SCHEMA}.{op.table}`")
    if op.column not in columns:
        raise Invalid(f"{where}: `{SCHEMA}.{op.table}` has no column `{op.column}`")
    if op.column in state.primary_keys.get(op.table, []):
        raise Invalid(
            f"{where}: `{op.column}` is in `{SCHEMA}.{op.table}`'s primary key, so it can't be"
            " dropped"
        )
    embedding = next(
        (c for (t, c), source in state.embeddings.items() if t == op.table and source == op.column),
        None,
    )
    if embedding is not None:
        raise Invalid(
            f"{where}: `{embedding}` is its embedding: drop `{embedding}` first (in this request"
            " or before)"
        )
    dropped = state.dropped_columns.setdefault(op.table, set())
    dropped_as = _dropped_as(op, now, op.column, dropped | set(columns))
    if dropped_as in dropped or dropped_as in columns:
        raise Invalid(f"{where}: `{SCHEMA}.{op.table}` already has a column `{dropped_as}`")
    not_null = state.not_null.get(op.table, set())
    checked = op.model_copy(
        update={
            "dropped_as": dropped_as,
            "not_null": op.column in not_null,
            "is_embedding": (op.table, op.column) in state.embeddings,
        }
    )
    del columns[op.column]
    not_null.discard(op.column)
    state.embeddings.pop((op.table, op.column), None)
    dropped.add(dropped_as)
    return checked


def _check_embedding(column: Column, columns: dict[str, str], table: str, where: str) -> None:
    if column.type != "embedding":
        return
    source = columns.get(column.embedding_of)
    if source != "text":
        raise Invalid(
            f"{where}: `{column.name}` is to be the embedding of `{column.embedding_of}`, which"
            f" must be a text column of `{SCHEMA}.{table}`"
        )


def _check_create_table(
    op: CreateTable, tables: dict[str, dict[str, str]], relations: set[str], where: str
) -> None:
    if op.name in relations:
        raise Invalid(f"{where}: `{SCHEMA}.{op.name}` already exists")
    names = [c.name for c in op.columns]
    if op.primary_key is None and "id" in names:
        names.append("id")  # reported as a duplicate below
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        hint = (
            " (a table without `primary_key` gets an `id` column of its own)"
            if "id" in duplicates and op.primary_key is None
            else ""
        )
        raise Invalid(f"{where}: more than one column named {', '.join(duplicates)}{hint}")
    columns = {c.name: c.pg_type for c in op.columns}
    if op.primary_key is not None:
        for key in op.primary_key:
            if key not in columns:
                raise Invalid(f"{where}: the primary key's column `{key}` isn't in the table")
            if columns[key].startswith("vector") or columns[key] == "jsonb":
                raise Invalid(f"{where}: `{key}` can't be in the primary key: it's {columns[key]}")
        if len(set(op.primary_key)) != len(op.primary_key):
            raise Invalid(f"{where}: the primary key names a column twice")
    else:
        columns = {"id": "bigint", **columns}
    for column in op.columns:
        _check_embedding(column, columns, op.name, where)
    tables[op.name] = columns
    # And the names Postgres gives its key's index and identity's sequence (unless they're
    # taken, when it picks others): later operations can't have them.
    relations.update({op.name, f"{op.name[:58]}_pkey"})
    if op.primary_key is None:
        relations.add(f"{op.name[:56]}_id_seq")


def _check_add_column(
    op: AddColumn, tables: dict[str, dict[str, str]], where: str, *, existing: bool
) -> None:
    columns = tables.get(op.table)
    if columns is None:
        raise Invalid(f"{where}: there's no table `{SCHEMA}.{op.table}`")
    if op.column.name in columns:
        raise Invalid(f"{where}: `{SCHEMA}.{op.table}` already has a column `{op.column.name}`")
    if existing and not op.column.nullable and op.column.default is None:
        raise Invalid(
            f"{where}: `{op.column.name}` isn't nullable, so it needs a default: the rows"
            f" `{SCHEMA}.{op.table}` already has need a value"
        )
    _check_embedding(op.column, columns, op.table, where)
    columns[op.column.name] = op.column.pg_type


def _index_name(op: CreateIndex, relations: set[str]) -> str:
    base, n = "_".join([op.table, *op.columns]), 1
    while True:
        suffix = "_idx" if n == 1 else f"_idx{n}"
        # Within Postgres's 63 characters, which it would otherwise cut it down to.
        name = base[: 63 - len(suffix)] + suffix
        if name not in relations:
            return name
        n += 1


def _check_create_index(
    op: CreateIndex, tables: dict[str, dict[str, str]], relations: set[str], where: str
) -> CreateIndex:
    columns = tables.get(op.table)
    if columns is None:
        raise Invalid(f"{where}: there's no table `{SCHEMA}.{op.table}`")
    for name in op.columns:
        if name not in columns:
            raise Invalid(f"{where}: `{SCHEMA}.{op.table}` has no column `{name}`")
    if len(set(op.columns)) != len(op.columns):
        raise Invalid(f"{where}: the index names a column twice")
    types = [columns[c] for c in op.columns]
    if op.unique and op.method != "btree":
        raise Invalid(f"{where}: only a btree index can be unique")
    if op.method == "btree" and any(t.startswith("vector") for t in types):
        raise Invalid(f"{where}: a btree index can't hold a vector column; use hnsw")
    if op.method in ("gin", "fulltext", "hnsw") and len(op.columns) != 1:
        raise Invalid(f"{where}: a {op.method} index is on one column")
    if op.method == "gin" and types[0] != "jsonb":
        raise Invalid(f"{where}: a gin index is for a jsonb column, not {types[0]}")
    if op.method == "fulltext" and types[0] != "text":
        raise Invalid(f"{where}: a fulltext index is for a text column, not {types[0]}")
    if op.method == "hnsw":
        match = re.fullmatch(r"vector\((\d+)\)", types[0])
        if match is None:
            raise Invalid(f"{where}: an hnsw index is for a vector column, not {types[0]}")
        if int(match[1]) > HNSW_MAX_DIMENSIONS:
            raise Invalid(
                f"{where}: an hnsw index takes vectors of at most {HNSW_MAX_DIMENSIONS} dimensions"
            )
    name = op.name or _index_name(op, relations)
    if name in relations:
        raise Invalid(f"{where}: `{SCHEMA}.{name}` already exists")
    relations.add(name)
    return op.model_copy(update={"name": name})


def summarize(change: SchemaChange) -> str:
    """What a spec does, in a line: for the store's history and the owner's list."""
    parts = []
    for op in change.ops:
        if isinstance(op, CreateTable):
            parts.append(f"create table {SCHEMA}.{op.name}")
        elif isinstance(op, AddColumn):
            parts.append(f"add column {op.column.name} to {SCHEMA}.{op.table}")
        elif isinstance(op, CreateIndex):
            parts.append(f"create index {op.name} on {SCHEMA}.{op.table}")
        elif isinstance(op, DropTable):
            parts.append(f"drop table {SCHEMA}.{op.table}")
        else:
            parts.append(f"drop column {op.column} from {SCHEMA}.{op.table}")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:]


# --- The store's schema -------------------------------------------------------------------------

# Every relation in `data` (tables, indexes, sequences, views...), with a table's columns.
_SCHEMA_SQL = """
    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
           coalesce(a.attnum = ANY(pk.indkey), false)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        AND c.relkind IN ('r', 'p')
    LEFT JOIN pg_index pk ON pk.indrelid = c.oid AND pk.indisprimary
    WHERE n.nspname = 'data'
    ORDER BY c.relname, a.attnum
"""
# And types of their own there, whose names a new table's would clash with.
_TYPES_SQL = """
    SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'data' AND t.typrelid = 0
"""
_EMBEDDINGS_SQL = "SELECT table_name, column_name, source_column FROM aios.embedding_columns"


def read_schema(pg: psycopg.Connection) -> StoreSchema:
    """The store's `data` schema, as specs are checked against it."""
    schema = StoreSchema()
    for relation, column, type_, not_null, in_key in pg.execute(_SCHEMA_SQL):
        schema.relations.add(relation)
        if column is None or relation.startswith(DROPPED_PREFIX):
            continue
        if column.startswith(DROPPED_PREFIX):
            schema.dropped_columns.setdefault(relation, set()).add(column)
            continue
        # pgvector's type, named with its schema unless that's on the search path.
        schema.tables.setdefault(relation, {})[column] = type_.removeprefix("extensions.")
        if not_null:
            schema.not_null.setdefault(relation, set()).add(column)
        if in_key:
            schema.primary_keys.setdefault(relation, []).append(column)
    schema.relations.update(name for (name,) in pg.execute(_TYPES_SQL))
    schema.embeddings = {(t, c): source for t, c, source in pg.execute(_EMBEDDINGS_SQL)}
    return schema


def read_revision(pg: psycopg.Connection) -> int:
    """The store's data schema revision: its latest change's (0 before any). Each applied
    change appends to `aios.schema_history`, which bumps it."""
    return pg.execute("SELECT coalesce(max(id), 0) FROM aios.schema_history").fetchone()[0]


# --- Requests -----------------------------------------------------------------------------------


def find(run_id: int, tool_call_id: str) -> SchemaChangeRequest | None:
    """The request a run's tool call made, if it made one."""
    with SessionLocal() as db:
        return db.scalar(
            select(SchemaChangeRequest).where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.tool_call_id == tool_call_id,
            )
        )


def propose(
    data_store_id: uuid.UUID,
    change: SchemaChange,
    reason: str,
    *,
    run_id: int | None,
    tool_call_id: str | None,
) -> SchemaChangeRequest:
    """Check `change` against the store's schema and record it as a pending request; raises
    `Invalid` if the schema doesn't take it, and as `store_session` does if the store can't be
    read."""
    with store_session(data_store_id, "ro") as session:
        pg = session.connection().connection.driver_connection
        # The revision first: a change committed in between makes the schema newer than the
        # revision, which only means it's checked again when it's applied.
        revision = read_revision(pg)
        schema = read_schema(pg)
    # The names drops rename to are the platform's to pick, never the model's.
    drops = (DropTable, DropColumn)
    fresh = [
        op.model_copy(update={"dropped_as": None}) if isinstance(op, drops) else op
        for op in change.ops
    ]
    checked = check(SchemaChange(ops=fresh), schema)
    request = SchemaChangeRequest(
        data_store_id=data_store_id,
        ops=checked.model_dump(mode="json", exclude_none=True)["ops"],
        sql=compile_sql(checked),
        summary=summarize(checked),
        reason=reason,
        requested_by_run=run_id,
        tool_call_id=tool_call_id,
        base_revision=revision,
    )
    with SessionLocal() as db:
        db.add(request)
        db.commit()
    return request


def _store_error(exc: psycopg.Error) -> str:
    """Why applying failed, for the owner and the model: Postgres's own message, but not one
    from connecting (which names the server)."""
    if exc.sqlstate and exc.diag.message_primary and not exc.sqlstate.startswith("08"):
        return exc.diag.message_primary
    return "The data store couldn't be reached"


class _Stale(Exception):
    pass


def _run(names: "StoreNames", request: SchemaChangeRequest) -> None:
    """Run the request's SQL on the store, as its owner, all or nothing; raises `_Stale` if the
    schema has moved on and no longer takes it."""
    from aios.datastores import dropped
    from aios.datastores.provision import as_owner

    timeout = f"{get_settings().schema_change_lock_timeout}ms"
    with as_owner(names) as conn, conn.transaction():
        conn.execute("SELECT set_config('lock_timeout', %s, true)", (timeout,))
        # One change at a time, each seeing the last: the revision can't move until commit.
        conn.execute("LOCK TABLE aios.schema_history IN EXCLUSIVE MODE")
        done = conn.execute(
            "SELECT 1 FROM aios.schema_history WHERE request_id = %s", (request.id,)
        ).fetchone()
        if done:  # applied already, by a run of this that stopped before recording it
            return
        spec = SchemaChange.model_validate({"ops": request.ops})
        if read_revision(conn) != request.base_revision:
            try:
                rechecked = check(spec, read_schema(conn))
            except Invalid as exc:
                raise _Stale(str(exc)) from exc
            # What it found to drop, say, has changed: the SQL approved isn't what it takes now.
            if compile_sql(rechecked) != request.sql:
                raise _Stale(
                    "the tables it changes have changed since it was asked for, so the SQL"
                    " approved isn't what it would take now"
                )
        conn.execute(request.sql)  # several statements: sent whole, with no parameters
        for table in _access_changed(spec):
            if read_exists(conn, table):
                dropped.grant_access(conn, names.rw, names.ro, table)
        conn.execute(
            "INSERT INTO aios.schema_history (request_id, summary, sql) VALUES (%s, %s, %s)",
            (request.id, request.summary, request.sql),
        )


def _access_changed(change: SchemaChange) -> list[str]:
    """The tables (as they're named once it's applied) whose login roles' access a change
    sets anew (`dropped.grant_access`): those it drops, drops columns from, or adds columns to
    (which may have dropped columns, so get their privileges column by column)."""
    tables: list[str] = []
    for op in change.ops:
        if isinstance(op, DropTable):
            tables.append(op.dropped_as)
        elif isinstance(op, (DropColumn, AddColumn)):
            tables.append(op.table)
    return list(dict.fromkeys(tables))


def read_exists(pg: psycopg.Connection, table: str) -> bool:
    """Whether `data.<table>` is there."""
    qualified = f"{_quote(SCHEMA)}.{_quote(table)}"
    return pg.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,)).fetchone()[0]


def apply(request_id: uuid.UUID) -> SchemaChangeStatus | None:
    """Apply an approved request to its store and record how it went (`applied`, `failed` or
    `stale`); returns that, or None if the request isn't approved (there's nothing to do)."""
    from aios.datastores.provision import StoreNames, provisioner_locked

    with SessionLocal() as db:
        request = db.get(SchemaChangeRequest, request_id)
        if request is None or request.status != SchemaChangeStatus.approved:
            return None
        store = db.get(DataStore, request.data_store_id)
        names = StoreNames.of(store)
    error = None
    try:
        # Not while the store is being migrated or provisioned.
        with provisioner_locked(request.data_store_id):
            _run(names, request)
        status = SchemaChangeStatus.applied
    except _Stale as exc:
        status, error = SchemaChangeStatus.stale, str(exc)
    except psycopg.Error as exc:
        status, error = SchemaChangeStatus.failed, _store_error(exc)
    except Exception:
        log.exception("Applying schema change request %s failed", request_id)
        status, error = SchemaChangeStatus.failed, "Applying it failed unexpectedly"
    with SessionLocal() as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.id == request_id,
                SchemaChangeRequest.status == SchemaChangeStatus.approved,
            )
            .values(
                status=status,
                error=error,
                applied_at=func.now() if status == SchemaChangeStatus.applied else None,
            )
        )
        db.commit()
    return status


def outcome(request: SchemaChangeRequest) -> dict[str, Any]:
    """What the run that asked is resumed with, and its tool call returns to the model."""
    status = request.status
    out: dict[str, Any] = {"status": status.value, "request_id": str(request.id)}
    if status == SchemaChangeStatus.applied:
        out["message"] = (
            f"The user approved it, and it's applied: {request.summary}. `describe_data`"
            " shows the store's tables as they are now."
        )
        if confirmation(SchemaChange.model_validate({"ops": request.ops})):
            out["message"] += (
                " What it dropped is kept for a while: the user can restore it from the Data"
                " page until it's purged."
            )
    elif status == SchemaChangeStatus.rejected:
        out["note"] = request.note
        out["message"] = "The user rejected it, and nothing changed" + (
            f". Their note: {request.note}" if request.note else "."
        )
    elif status == SchemaChangeStatus.stale:
        out["error"] = request.error
        out["message"] = (
            "The store's schema changed after the request was made, and it no longer applies,"
            f" so nothing changed: {request.error}"
        )
    elif status == SchemaChangeStatus.failed:
        out["error"] = request.error
        out["message"] = (
            f"The user approved it, but applying it failed, and nothing changed: {request.error}"
        )
    else:  # expired or cancelled
        out["message"] = f"The request was {status.value} without being applied: nothing changed."
    return out
