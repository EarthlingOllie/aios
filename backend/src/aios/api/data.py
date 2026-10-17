"""The Data page's Tables: the user's store's own tables, and their rows. Read-only, and no SQL
is accepted: tables and columns are named, and checked against the store's catalog."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from aios.auth import CurrentUser
from aios.config import get_settings
from aios.datastores import data_store_for, tables

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
    key: str
    values: list[str | None]


class RowsOut(BaseModel):
    columns: list[str]
    rows: list[RowOut]
    offset: int
    has_more: bool


@router.get("/tables", response_model=TablesOut)
def list_tables(user: CurrentUser):
    found, truncated = tables.list_tables(data_store_for(user.id))
    return TablesOut(
        tables=[TableOut.model_validate(t, from_attributes=True) for t in found],
        truncated=truncated,
    )


@router.get("/tables/{table}/rows", response_model=RowsOut)
def read_rows(
    table: str,
    user: CurrentUser,
    offset: Annotated[int, Query(ge=0, le=10**15)] = 0,
    # At most `query_data`'s row cap: more is taken as that many.
    limit: Annotated[int, Query(ge=1)] = 50,
    sort: str | None = None,
    order: Literal["asc", "desc"] = "asc",
):
    try:
        page = tables.read_rows(
            data_store_for(user.id),
            table,
            offset=offset,
            limit=min(limit, get_settings().query_data_max_rows),
            sort=sort,
            descending=order == "desc",
        )
    except tables.TableNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such table") from exc
    except tables.ColumnNotFound as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The table has no column {exc.args[0]!r}"
        ) from exc
    return RowsOut(
        columns=page.columns,
        rows=[RowOut(key=r.key, values=r.values) for r in page.rows],
        offset=offset,
        has_more=page.has_more,
    )
