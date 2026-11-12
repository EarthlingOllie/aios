"""The Data page's Tables: the organisation's store's own tables, their rows, and who may write
them. No SQL is accepted: tables and columns are named, and checked against the store's catalog.

Rows are read-only here. Each table's access (its owner and shares, `aios.datastores.ownership`)
is listed with it, and editors and admins change it: assign, transfer or clear its owner, and
share and unshare it. Each change answers with the table's access, audited; 403 for a role the
approval rules don't allow, 404 for a table or agent there isn't, 409 for an archived agent, a
duplicate share, or a share with the owner (or of a table with no owner)."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from aios.auth import CurrentDataStore, CurrentStore, CurrentUser
from aios.config import get_settings
from aios.datastores import agent_roles, ownership, store_session, tables
from aios.datastores.connections import store_id_of
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api/data")


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    description: str | None
    embedding: bool


class AgentRefOut(BaseModel):
    id: int
    name: str
    # Archived: a table it owns is read-only until it's transferred.
    archived: bool


class AccessOut(BaseModel):
    # None: no owner, so any agent with `write_data` may write it.
    owner: AgentRefOut | None
    # The agents it's shared with, which may write it as its owner may.
    shares: list[AgentRefOut]
    # A change the database hasn't applied yet (never, until per-agent roles carry them).
    pending: bool


class TableAccessOut(AccessOut):
    name: str


class TableOut(AccessOut):
    name: str
    description: str | None
    columns: list[ColumnOut]
    row_count: int | None
    row_count_estimated: bool


class OwnerIn(BaseModel):
    # None clears it.
    agent_id: int | None


class ShareIn(BaseModel):
    agent_id: int


class TablesOut(BaseModel):
    tables: list[TableOut]
    # Some tables were left out, to keep the list within the byte cap.
    truncated: bool


class RowOut(BaseModel):
    # Names the row to `GET .../value`.
    key: str
    # As text (null for NULL), each cut to a preview.
    values: list[str | None]
    # The values that were cut, by their place in `values`.
    truncated: list[int]


class ValueOut(BaseModel):
    value: str | None
    # Cut short at the byte cap.
    truncated: bool


class RowsOut(BaseModel):
    columns: list[str]
    rows: list[RowOut]
    offset: int
    has_more: bool


@router.get("/tables", response_model=TablesOut, dependencies=[allow(Action.read)])
def list_tables(store_id: CurrentDataStore):
    with _refusals():
        found, truncated = tables.list_tables(store_id)
    with store_session(store_id, "ro") as store:
        access = ownership.access(store, [t.name for t in found])
    return TablesOut(
        tables=[
            TableOut.model_validate(
                t.listed()
                | {"row_count": t.row_count, "row_count_estimated": t.row_count_estimated}
                | _access(access[t.name])
            )
            for t in found
        ],
        truncated=truncated,
    )


def _access(access: ownership.Access) -> dict:
    return {
        "owner": None if access.owner is None else vars(access.owner),
        "shares": [vars(a) for a in access.shares],
        "pending": access.pending,
    }


@router.put(
    "/tables/{table:path}/owner", response_model=TableAccessOut, dependencies=[allow(Action.tables)]
)
def set_owner(table: str, body: OwnerIn, user: CurrentUser, role: CurrentRole, store: CurrentStore):
    """Assign, transfer or clear (`agent_id` null) the table's owner."""
    with _access_refusals():
        access = ownership.set_owner(store, table, body.agent_id, user.id, role)
    store.commit()
    # Applied on the urgent lane: pending until then.
    agent_roles.enqueue(store_id_of(store))
    return TableAccessOut(name=table, **_access(access))


@router.post(
    "/tables/{table:path}/shares",
    response_model=TableAccessOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.tables)],
)
def share(table: str, body: ShareIn, user: CurrentUser, store: CurrentStore):
    """Share the table with an agent: it may write it as its owner may."""
    with _access_refusals():
        access = ownership.share(store, table, body.agent_id, user.id)
    store.commit()
    # Applied on the urgent lane: pending until then.
    agent_roles.enqueue(store_id_of(store))
    return TableAccessOut(name=table, **_access(access))


@router.delete(
    "/tables/{table:path}/shares/{agent_id}",
    response_model=TableAccessOut,
    dependencies=[allow(Action.tables)],
)
def unshare(table: str, agent_id: int, user: CurrentUser, store: CurrentStore):
    """Stop sharing the table with an agent."""
    with _access_refusals():
        access = ownership.unshare(store, table, agent_id, user.id)
    store.commit()
    # Applied on the urgent lane: pending until then.
    agent_roles.enqueue(store_id_of(store))
    return TableAccessOut(name=table, **_access(access))


@contextmanager
def _access_refusals() -> Iterator[None]:
    try:
        yield
    except ownership.TableNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such table") from exc
    except ownership.AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such agent") from exc
    except ownership.ShareNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The table isn't shared with that agent"
        ) from exc
    except ownership.Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ownership.NeedsRole as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.get("/tables/{table:path}/rows", response_model=RowsOut, dependencies=[allow(Action.read)])
def read_rows(
    table: str,
    store_id: CurrentDataStore,
    offset: Annotated[int, Query(ge=0, le=10**15)] = 0,
    # At most `query_data`'s row cap: more is taken as that many.
    limit: Annotated[int, Query(ge=1)] = 50,
    sort: str | None = None,
    order: Literal["asc", "desc"] = "asc",
):
    with _refusals():
        page = tables.read_rows(
            store_id,
            table,
            offset=offset,
            limit=min(limit, get_settings().query_data_max_rows),
            sort=sort,
            descending=order == "desc",
        )
    return RowsOut(
        columns=page.columns,
        rows=[RowOut.model_validate(r, from_attributes=True) for r in page.rows],
        offset=offset,
        has_more=page.has_more,
    )


@router.get(
    "/tables/{table:path}/value", response_model=ValueOut, dependencies=[allow(Action.read)]
)
def read_value(table: str, row: str, column: str, store_id: CurrentDataStore):
    """One value of a row in full (as far as the byte cap), where the page cut it short."""
    with _refusals():
        value, truncated = tables.read_value(store_id, table, row, column)
    return ValueOut(value=value, truncated=truncated)


@contextmanager
def _refusals() -> Iterator[None]:
    """Names the catalog doesn't have as 404 (a table, a row) or 422 (a column, a row key); a
    column that can't be sorted by, or a row too wide to show, as 422; and a read past the time
    limit as 504."""
    try:
        yield
    except tables.TooSlow as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "Reading the table took too long, and was stopped."
        ) from exc
    except tables.CantSort as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The column {exc.args[0]!r} can't be sorted by"
        ) from exc
    except tables.TableNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such table") from exc
    except tables.RowNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such row (any more)") from exc
    except tables.ColumnNotFound as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The table has no column {exc.args[0]!r}"
        ) from exc
    except tables.BadRowKey as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not a row's key") from exc
    except tables.RowTooWide as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A row of this table is too wide to show here, even with its values cut short.",
        ) from exc
