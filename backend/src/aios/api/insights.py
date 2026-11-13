"""The Insights page's panels (`aios.insights`): every member reads them, for a range of days
(`start` to `end`, UTC, both included; by default the last 30 days) and, for the panels that
group (`activity`, `usage`), a grouping by agent, workflow, cause or person.

Usage by person is admins' only, unless an admin has turned on showing it to everyone
(`PUT .../organisation/insights-people`, `aios.api.organisation`): anyone else asking for the
person grouping gets a 403 saying so.

Costs are estimates in GBP from the price table, never a bill. Turns from before counts were
kept are counted as "not tracked", with null counters.

Charts (`aios.charts`): any member asks for one in words (`POST .../ask`: rate-limited per
person, a 429 past it; a 409 with why at the budget or their allowance, as starting a run by
hand), and may save one for the organisation (`.../charts`), its spec and SQL checked again.
Viewing a saved chart re-runs its SQL (a 409 saying its query no longer works if it fails).
Only its creator or an admin renames or deletes it (a 403 for anyone else); saves, renames and
deletions are audited.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import audit, charts, guards, insights, ratelimits
from aios.api.provenance import UserOut, people
from aios.api.sources import SourceOut
from aios.auth import CurrentDataStore, CurrentOrganisation, CurrentStore, CurrentUser
from aios.config import get_settings
from aios.datastores import store_id_of, store_session
from aios.datastores.models import InsightChart
from aios.db import DB, DbId
from aios.models import Role
from aios.permissions import Action, CurrentRole, allow

router = APIRouter(prefix="/api/insights")

# The longest range one read covers.
MAX_DAYS = 366

PEOPLE_HIDDEN = (
    "Usage by person is shown to admins only. An admin can show it to everyone in the"
    " organisation's settings."
)

GroupQuery = Annotated[Literal["agent", "workflow", "cause", "person"], Query()]


class DayOut(BaseModel):
    day: date
    total: int
    succeeded: int
    failed: int
    # Turns from before counts were kept.
    not_tracked: int


class SeriesOut(BaseModel):
    # The agent's, workflow's or person's id, or the cause; null for none (a workflow run has
    # no agent, a schedule's run no person).
    key: int | str | None
    name: str | None
    days: list[DayOut]


class ActivityOut(BaseModel):
    group: str
    start: date
    end: date
    turns: list[SeriesOut]
    workflow_runs: list[SeriesOut]


class UsageRowOut(BaseModel):
    key: int | str | None
    name: str | None
    turns: int
    # Of those, from before counts were kept: their tokens and tool calls aren't known.
    not_tracked: int
    # Null when no turn of the group's is tracked: "not tracked", not zero.
    model_calls: int | None
    tool_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    # Estimated, in GBP; null if nothing in it was priced.
    cost: float | None
    unpriced: int


class TopAgentOut(BaseModel):
    key: int
    name: str
    turns: int


class TopToolOut(BaseModel):
    tool: str
    calls: int


class UsagePanelOut(BaseModel):
    group: str
    start: date
    end: date
    currency: Literal["GBP"] = "GBP"
    rows: list[UsageRowOut]
    top_agents: list[TopAgentOut]
    top_tools: list[TopToolOut]


class SlowWorkflowOut(BaseModel):
    workflow_id: int
    name: str
    runs: int
    # Time running, without waiting for approvals.
    mean_seconds: float
    max_seconds: float
    waited_seconds: float


class SlowNodeOut(BaseModel):
    workflow_id: int
    name: str
    # Its path of node keys, "/"-joined.
    node: str
    kind: str
    runs: int
    mean_seconds: float
    max_seconds: float


class FailureOut(BaseModel):
    workflow_id: int
    name: str
    node: str
    kind: str
    failures: int
    latest_at: datetime | None
    # The latest run that failed there: its page shows the error.
    latest_run_id: int


class WorkflowsOut(BaseModel):
    start: date
    end: date
    slowest_workflows: list[SlowWorkflowOut]
    slowest_nodes: list[SlowNodeOut]
    failures: list[FailureOut]


class ApprovalKindOut(BaseModel):
    kind: str
    total: int
    approved: int
    rejected: int
    # Counted apart: an expiry isn't a decision.
    expired: int
    cancelled: int
    pending: int
    # How long approved and rejected ones waited to be decided.
    mean_wait_seconds: float | None
    max_wait_seconds: float | None


class ApprovalsOut(BaseModel):
    start: date
    end: date
    kinds: list[ApprovalKindOut]


class ChatActivityOut(BaseModel):
    chat_id: int
    name: str
    archived: bool
    # People's messages, and agents' and workflows'.
    messages: int
    agent_messages: int
    # Mentions of agents and workflows, and how the runs they started went.
    mentions: int
    succeeded: int
    failed: int
    refused: int
    in_progress: int


class ChatsOut(BaseModel):
    start: date
    end: date
    chats: list[ChatActivityOut]


class SampleOut(BaseModel):
    measured_at: datetime
    db_bytes: int
    files_bytes: int


class StorageOut(BaseModel):
    start: date
    end: date
    db_quota_bytes: int
    files_quota_bytes: int
    db_bytes: int | None
    files_bytes: int | None
    measured_at: datetime | None
    samples: list[SampleOut]


class SourcesOut(BaseModel):
    # False while the organisation has only Uploads: the panel is hidden.
    shown: bool
    sources: list[SourceOut]


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=29)
    if start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The range ends before it starts."
        )
    if (end - start).days >= MAX_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A range covers at most {MAX_DAYS} days."
        )
    return start, end


def people_visible(org: Any, role: Role) -> bool:
    """Whether a member with `role` sees usage by person."""
    return role == Role.admin or bool(org.insights_people_visible)


def _grouped(name, db, org, role, store_id, group, start, end) -> dict:
    if group == "person" and not people_visible(org, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, PEOPLE_HIDDEN)
    start, end = _range(start, end)
    data = insights.panel(store_id, name, start, end, group)
    if group == "person":
        keys: list = [row["key"] for row in data.get("rows", [])]
        for series in data.get("turns", []) + data.get("workflow_runs", []):
            keys.append(series["key"])
        names = {i: p.name for i, p in people(db, org.id, keys).items()}
        data = {
            **data,
            **{
                part: [{**row, "name": names.get(row["key"])} for row in data[part]]
                for part in ("rows", "turns", "workflow_runs")
                if part in data
            },
        }
    return {**data, "group": group, "start": start, "end": end}


Start = Annotated[date | None, Query(description="From this day (UTC).")]
End = Annotated[date | None, Query(description="To this day, inclusive (UTC).")]


@router.get("/activity", response_model=ActivityOut, dependencies=[allow(Action.read)])
def read_activity(
    db: DB,
    org: CurrentOrganisation,
    role: CurrentRole,
    store_id: CurrentDataStore,
    group: GroupQuery = "agent",
    start: Start = None,
    end: End = None,
):
    """Turns and workflow runs by day, grouped, with how many succeeded and failed."""
    return _grouped("activity", db, org, role, store_id, group, start, end)


@router.get("/usage", response_model=UsagePanelOut, dependencies=[allow(Action.read)])
def read_usage(
    db: DB,
    org: CurrentOrganisation,
    role: CurrentRole,
    store_id: CurrentDataStore,
    group: GroupQuery = "agent",
    start: Start = None,
    end: End = None,
):
    """Turns, tokens, tool calls and estimated cost (GBP), grouped; the most used agents and
    tools."""
    return _grouped("usage", db, org, role, store_id, group, start, end)


def _plain(name, store_id, start, end) -> dict:
    start, end = _range(start, end)
    return {**insights.panel(store_id, name, start, end), "start": start, "end": end}


@router.get("/workflows", response_model=WorkflowsOut, dependencies=[allow(Action.read)])
def read_workflows(store_id: CurrentDataStore, start: Start = None, end: End = None):
    """The slowest workflows and nodes, and where runs fail."""
    return _plain("workflows", store_id, start, end)


@router.get("/approvals", response_model=ApprovalsOut, dependencies=[allow(Action.read)])
def read_approvals(store_id: CurrentDataStore, start: Start = None, end: End = None):
    """Approvals by kind: how they were decided, and how long they waited."""
    return _plain("approvals", store_id, start, end)


@router.get("/chats", response_model=ChatsOut, dependencies=[allow(Action.read)])
def read_chats(store_id: CurrentDataStore, start: Start = None, end: End = None):
    """The busiest chats: messages, mentions and how they went."""
    return _plain("chats", store_id, start, end)


@router.get("/storage", response_model=StorageOut, dependencies=[allow(Action.read)])
def read_storage(store_id: CurrentDataStore, start: Start = None, end: End = None):
    """The store's usage over time against its caps."""
    return _plain("storage", store_id, start, end)


@router.get("/sources", response_model=SourcesOut, dependencies=[allow(Action.read)])
def read_sources(store_id: CurrentDataStore):
    """Each source's health, as the Sources tab shows it; not shown with only Uploads."""
    today = datetime.now(UTC).date()
    return insights.panel(store_id, "sources", today, today)


# --- Charts --------------------------------------------------------------------------------------

NO_LONGER_WORKS = "This chart's query no longer works: {reason}"
TOO_MANY_QUESTIONS = (
    "You've asked for too many charts. Try again in {retry} seconds; nothing was asked."
)


class AskIn(BaseModel):
    question: str = Field(max_length=charts.MAX_QUESTION)

    @field_validator("question")
    @classmethod
    def _words(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Ask a question.")
        return value


class AskOut(BaseModel):
    question: str
    title: str | None
    # Always shown to the asker, with or without a chart.
    sql: str | None
    # Vega-Lite, without data: `data` is drawn inline.
    spec: dict | None
    columns: list[str] | None
    data: list[dict] | None
    # Cut short at the row or byte cap: drawn, and marked as such.
    truncated: bool
    # Why there's no chart (the spec or query failed, after one retry); null if there is one.
    error: str | None


def _charge_checks(store_id: uuid.UUID, organisation_id: int, user_id: int) -> None:
    """The rate, then the budget: a 429 past the person's questions an hour, a 409 at the
    budget or their allowance; before any model call."""
    ratelimits.throttle(
        "chart-questions",
        f"{get_settings().chart_questions_per_hour}/hour",
        f"{organisation_id}:{user_id}",
        detail=TOO_MANY_QUESTIONS,
    )
    with store_session(store_id) as store:
        guards.refuse_at_budget(store, organisation_id, user_id)


@router.post("/ask", response_model=AskOut, dependencies=[allow(Action.post)])
async def ask_for_chart(
    body: AskIn, user: CurrentUser, org: CurrentOrganisation, store_id: CurrentDataStore
):
    """Ask the insights agent for a chart: its title, SQL, spec and data (marked if
    truncated), or an error with the SQL. A 429 past the rate limit; a 409 at the budget. No
    store connection is held while the agent thinks."""
    await run_in_threadpool(_charge_checks, store_id, org.id, user.id)
    try:
        asked = await charts.ask(store_id, org.id, user.id, body.question)
    except charts.AgentFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return AskOut.model_validate(asked, from_attributes=True)


def _a_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Give the chart a title.")
    return value


class ChartRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    _title = field_validator("title")(_a_title)


class ChartIn(ChartRename):
    question: str = Field(default="", max_length=charts.MAX_QUESTION)
    sql: str = Field(min_length=1, max_length=20_000)
    spec: dict


class ChartOut(BaseModel):
    id: int
    title: str
    question: str
    sql: str
    spec: dict
    created_by: UserOut | None
    created_at: datetime
    updated_at: datetime
    # Whether the caller may rename or delete it: its creator, or an admin.
    can_change: bool


class ChartDataOut(BaseModel):
    columns: list[str]
    data: list[dict]
    truncated: bool


def _may_change(chart: InsightChart, user_id: int, role: Role) -> bool:
    return role == Role.admin or chart.created_by == user_id


def _charts_out(db, org_id: int, user_id: int, role: Role, rows: list[InsightChart]) -> list:
    names = people(db, org_id, [c.created_by for c in rows])
    return [
        ChartOut(
            id=c.id,
            title=c.title,
            question=c.question,
            sql=c.sql,
            spec=c.spec,
            created_by=names.get(c.created_by),
            created_at=c.created_at,
            updated_at=c.updated_at,
            can_change=_may_change(c, user_id, role),
        )
        for c in rows
    ]


def _chart(store: Session, chart_id: int) -> InsightChart:
    chart = store.get(InsightChart, chart_id)
    if chart is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such chart.")
    return chart


@router.get("/charts", response_model=list[ChartOut], dependencies=[allow(Action.read)])
def list_charts(
    db: DB, user: CurrentUser, org: CurrentOrganisation, role: CurrentRole, store: CurrentStore
):
    """The organisation's saved charts, newest first."""
    rows = list(store.scalars(select(InsightChart).order_by(InsightChart.id.desc())))
    return _charts_out(db, org.id, user.id, role, rows)


@router.post(
    "/charts",
    response_model=ChartOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def save_chart(
    body: ChartIn,
    db: DB,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    store: CurrentStore,
):
    """Save a chart for the organisation: a 422 if its spec or SQL doesn't pass again."""
    if problem := charts.check_spec(body.spec):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    try:
        charts.run_query(store_id_of(store), body.sql)
    except charts.ChartQueryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"The query failed: {exc}"
        ) from exc
    chart = InsightChart(
        title=body.title,
        question=body.question.strip(),
        sql=body.sql,
        spec=charts.drawable(body.spec),
        created_by=user.id,
    )
    store.add(chart)
    store.flush()
    audit.record(
        store,
        audit.person(user.id),
        "insight_chart.saved",
        "insight_chart",
        chart.id,
        {"title": chart.title},
    )
    store.commit()
    store.refresh(chart)
    return _charts_out(db, org.id, user.id, role, [chart])[0]


@router.get(
    "/charts/{chart_id}/data", response_model=ChartDataOut, dependencies=[allow(Action.read)]
)
def chart_data(chart_id: DbId, store: CurrentStore):
    """The chart's rows, from re-running its SQL on current data (cached for a few minutes): a
    409 if its query no longer works."""
    chart = _chart(store, chart_id)
    store_id = store_id_of(store)
    sql = chart.sql
    store.rollback()
    key = (store_id, "chart", chart_id, hashlib.sha256(sql.encode()).hexdigest())
    try:
        rows = insights.cached(key, lambda: charts.run_query(store_id, sql))
    except charts.ChartQueryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, NO_LONGER_WORKS.format(reason=exc)) from exc
    return ChartDataOut(columns=rows.columns, data=charts.records(rows), truncated=rows.truncated)


def _changeable(store: Session, chart_id: int, user_id: int, role: Role) -> InsightChart:
    chart = _chart(store, chart_id)
    if not _may_change(chart, user_id, role):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the chart's creator or an admin can change it."
        )
    return chart


@router.patch("/charts/{chart_id}", response_model=ChartOut, dependencies=[allow(Action.read)])
def rename_chart(
    chart_id: DbId,
    body: ChartRename,
    db: DB,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    store: CurrentStore,
):
    """Rename a saved chart: its creator or an admin only (a 403 for anyone else)."""
    chart = _changeable(store, chart_id, user.id, role)
    before = chart.title
    chart.title = body.title
    audit.record(
        store,
        audit.person(user.id),
        "insight_chart.renamed",
        "insight_chart",
        chart.id,
        {"from": before, "to": chart.title},
    )
    store.commit()
    store.refresh(chart)
    return _charts_out(db, org.id, user.id, role, [chart])[0]


@router.delete(
    "/charts/{chart_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.read)],
)
def delete_chart(chart_id: DbId, user: CurrentUser, role: CurrentRole, store: CurrentStore):
    """Delete a saved chart: its creator or an admin only (a 403 for anyone else)."""
    chart = _changeable(store, chart_id, user.id, role)
    audit.record(
        store,
        audit.person(user.id),
        "insight_chart.deleted",
        "insight_chart",
        chart.id,
        {"title": chart.title},
    )
    store.delete(chart)
    store.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
