"""Usage totals, for admins (`aios.usage`): the ledger's model calls, tokens, embeddings, web
searches, sandbox time and estimated cost in pounds sterling, grouped by agent, workflow, person
or day, over a range of days."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from aios import usage
from aios.api.provenance import people
from aios.auth import CurrentOrganisation, CurrentStore
from aios.datastores.models import Agent, Workflow
from aios.db import DB
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")

# The longest range one read covers.
MAX_DAYS = 366


class UsageTotalOut(BaseModel):
    # The agent's, workflow's or person's id, or the day (YYYY-MM-DD); null for what has none
    # (the handoff has no workflow, a schedule's run no person, say).
    key: int | str | None
    # The agent's, workflow's or person's name as it is now; the day itself for a day.
    name: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    embedded_texts: float
    web_searches: float
    # Tool calls the ledger counts (each `post_message` post).
    tool_calls: float
    sandbox_seconds: float
    # Estimated, in GBP; null if nothing in it was priced.
    cost: float | None
    # Entries with no price (a model missing from the price table).
    unpriced: int


class UsageOut(BaseModel):
    currency: Literal["GBP"] = "GBP"
    group: Literal["agent", "workflow", "person", "day"]
    start: date
    end: date
    totals: list[UsageTotalOut]


@router.get("/usage", response_model=UsageOut, dependencies=[allow(Action.usage)])
def read_usage(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    group: Annotated[Literal["agent", "workflow", "person", "day"], Query()] = "agent",
    start: Annotated[date | None, Query(description="From this day (UTC).")] = None,
    end: Annotated[date | None, Query(description="To this day, inclusive (UTC).")] = None,
):
    """Totals by `group` from `start` to `end` (by default the last 30 days, today included)."""
    today = datetime.now(UTC).date()
    end = end or today
    start = start or end - timedelta(days=29)
    if start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The range ends before it starts."
        )
    if (end - start).days >= MAX_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A range covers at most {MAX_DAYS} days."
        )
    rows = usage.totals(store, group, start, end)
    keys = [r["key"] for r in rows if r["key"] is not None]
    names: dict = {}
    if group in ("agent", "workflow") and keys:
        model = Agent if group == "agent" else Workflow
        names = dict(store.execute(select(model.id, model.name).where(model.id.in_(keys))).all())
    elif group == "person" and keys:
        names = {i: p.name for i, p in people(db, org.id, keys).items()}
    totals = []
    for row in rows:
        key = row["key"].isoformat() if isinstance(row["key"], date) else row["key"]
        name = key if group == "day" else names.get(row["key"])
        totals.append(UsageTotalOut(**{**row, "key": key, "name": name}))
    return UsageOut(group=group, start=start, end=end, totals=totals)
