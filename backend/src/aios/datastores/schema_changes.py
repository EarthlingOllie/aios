"""Schema change requests: how an agent changes the tables in its store's `data` schema.

The model never sends SQL. It sends a spec (`SchemaChange`): a list of additive operations
(`create_table`, `add_column`, `create_index`), each naming tables, columns and types from an
allowlist. The spec is checked against the store's current schema (`check`) and compiled to
SQL (`compile_sql`) with every identifier quoted; that SQL is what the owner approves, and
exactly what runs. Only the `data` schema is touched, apart from the platform's own
bookkeeping: `aios.embedding_columns`, which marks embedding columns for `write_data`.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from aios.datastores.models import EMBEDDING_DIM

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


Operation = Annotated[CreateTable | AddColumn | CreateIndex, Field(discriminator="op")]


class SchemaChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ops: list[Operation] = Field(min_length=1, max_length=20)


class Invalid(ValueError):
    """A spec the store's schema doesn't take: the message says why, for the model."""


@dataclass
class StoreSchema:
    """What a spec is checked against: the `data` schema's tables, each column's type (as
    `format_type` writes it), and every name taken in the schema (tables, indexes,
    sequences...)."""

    tables: dict[str, dict[str, str]] = field(default_factory=dict)
    relations: set[str] = field(default_factory=set)


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


def compile_sql(change: SchemaChange) -> str:
    """The SQL a checked spec (`check`) runs: what the owner approves."""
    statements: list[str] = []
    for op in change.ops:
        if isinstance(op, CreateTable):
            statements += _create_table_sql(op)
        elif isinstance(op, AddColumn):
            statements += _add_column_sql(op)
        else:
            statements += _create_index_sql(op)
    return "\n".join(statements)


def check(change: SchemaChange, schema: StoreSchema) -> SchemaChange:
    """The spec, checked against `schema` op by op (each seeing the ones before it), with
    the index names it left out filled in; raises `Invalid` if the schema doesn't take it."""
    tables = {name: dict(columns) for name, columns in schema.tables.items()}
    relations = set(schema.relations)
    checked: list[CreateTable | AddColumn | CreateIndex] = []
    for i, op in enumerate(change.ops, start=1):
        where = f"Operation {i} ({op.op})"
        if isinstance(op, CreateTable):
            _check_create_table(op, tables, relations, where)
        elif isinstance(op, AddColumn):
            _check_add_column(op, tables, where)
        else:
            op = _check_create_index(op, tables, relations, where)
        checked.append(op)
    return SchemaChange(ops=checked)


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
    relations.add(op.name)


def _check_add_column(op: AddColumn, tables: dict[str, dict[str, str]], where: str) -> None:
    columns = tables.get(op.table)
    if columns is None:
        raise Invalid(f"{where}: there's no table `{SCHEMA}.{op.table}`")
    if op.column.name in columns:
        raise Invalid(f"{where}: `{SCHEMA}.{op.table}` already has a column `{op.column.name}`")
    _check_embedding(op.column, columns, op.table, where)
    columns[op.column.name] = op.column.pg_type


def _index_name(op: CreateIndex, relations: set[str]) -> str:
    base = "_".join([op.table, *op.columns])[:59]
    name, n = f"{base}_idx", 2
    while name in relations or name in RESERVED:
        name = f"{base}_idx{n}"
        n += 1
    return name


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
