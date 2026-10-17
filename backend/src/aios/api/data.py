"""The Data page's Tables: the user's store's own tables, and their rows. Read-only, and no SQL
is accepted: tables and columns are named, and checked against the store's catalog."""

from fastapi import APIRouter
from pydantic import BaseModel

from aios.auth import CurrentUser
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


@router.get("/tables", response_model=TablesOut)
def list_tables(user: CurrentUser):
    found, truncated = tables.list_tables(data_store_for(user.id))
    return TablesOut(
        tables=[TableOut.model_validate(t, from_attributes=True) for t in found],
        truncated=truncated,
    )
