"""The Data page's Tables: the organisation's store's own tables, and their rows. Read-only, and
no SQL is accepted: tables and columns are named, and checked against the store's catalog."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from aios.auth import CurrentDataStore
from aios.config import get_settings
from aios.datastores import tables

router = APIRouter(prefix="/api/data")


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    description: str | None
    embedding: bool


class TableOut(BaseModel):
    name: str
    description: str | None
    columns: list[ColumnOut]
    row_count: int | None
    row_count_estimated: bool


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


@router.get("/tables", response_model=TablesOut)
def list_tables(store_id: CurrentDataStore):
    with _refusals():
        found, truncated = tables.list_tables(store_id)
    return TablesOut(
        tables=[TableOut.model_validate(t, from_attributes=True) for t in found],
        truncated=truncated,
    )


@router.get("/tables/{table:path}/rows", response_model=RowsOut)
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


@router.get("/tables/{table:path}/value", response_model=ValueOut)
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
