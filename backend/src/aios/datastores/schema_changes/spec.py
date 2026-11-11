"""What the model sends (`SchemaChange`): its operations, and how a spec is checked against a
store's schema (`check`) and summarised."""

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

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
    """Renames the table out of the way (`dropped_as`) rather than dropping it: a member can
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


def _literal(value: str) -> str:
    """A string constant (with `standard_conforming_strings`, Postgres's default)."""
    return "'" + value.replace("'", "''") + "'"


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
    # A live embedding of it (one that's dropped too keeps its marker, but needs nothing).
    embedding = next(
        (
            c
            for (t, c), source in state.embeddings.items()
            if t == op.table and source == op.column and not c.startswith(DROPPED_PREFIX)
        ),
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
    # Its marker stays, under its new name (and follows the table if that's dropped).
    source = state.embeddings.pop((op.table, op.column), None)
    if source is not None:
        state.embeddings[op.table, dropped_as] = source
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
    """What a spec does, in a line: for the store's history and the members' list."""
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
