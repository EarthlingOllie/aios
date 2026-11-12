"""The Insights page's panels (`aios.insights`): every member reads them, for a range of days
(`start` to `end`, UTC, both included; by default the last 30 days) and, for the panels that
group (`activity`, `usage`), a grouping by agent, workflow, cause or person.

Usage by person is admins' only, unless an admin has turned on showing it to everyone
(`PUT .../organisation/insights-people`, `aios.api.organisation`): anyone else asking for the
person grouping gets a 403 saying so.

Costs are estimates in GBP from the price table, never a bill. Turns from before counts were
kept are counted as "not tracked", with null counters.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from aios import insights
from aios.api.provenance import people
from aios.api.sources import SourceOut
from aios.auth import CurrentDataStore, CurrentOrganisation
from aios.db import DB
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
