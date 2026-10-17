import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from anyio import CancelScope
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from aios import checkpoints, tracing, ui_stream
from aios.agent import ChatDeps
from aios.api.skills import lock_live, lock_live_skills, unique_name
from aios.api.triggers import delete_triggers
from aios.api.workflows import workflows_using
from aios.auth import CurrentUser
from aios.datastores import data_store_for
from aios.db import DB, DbId, SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentVersion,
    AgentVersionTool,
    Skill,
    SkillVersion,
    Trigger,
    WorkflowNode,
    agent_version_skills,
)
from aios.runner import build_agent, run_title, version_tools
from aios.sandbox.sessions import (
    SandboxNotConfigured,
    manager,
    run_sandboxes,
    stop_sandboxes,
    worth_prewarming,
)
from aios.tools import (
    TOOLS,
    AgentTool,
    ToolError,
    check_stored_tool,
    stored_settings,
    validate_tool,
)
from aios.versioning import save_agent_version

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

AGENT_NAME_INDEX = "uq_agents_owner_id_name_live"


class AgentToolIn(BaseModel):
    key: str
    # Missing settings take the tool's defaults.
    config: dict[str, Any] = {}


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    base_prompt: str = ""
    # Specific skill versions, in order. The UI keeps already-attached skills on their pinned
    # version and adds new ones at their latest; updating a skill is an explicit change.
    skill_version_ids: list[DbId] = []
    # Tools from the library (GET /api/tools), in order, each with its settings.
    tools: list[AgentToolIn] = []
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class AgentSkillOut(BaseModel):
    skill_id: int
    skill_version_id: int
    name: str
    description: str
    version: int
    # The skill's current version, or None if the skill has since been deleted.
    latest_version: int | None
    latest_version_id: int | None


class AgentToolOut(BaseModel):
    key: str
    name: str
    description: str
    config: dict[str, Any]
    # False if the tool has since been removed from the library.
    available: bool


class AgentVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    skill_count: int
    tool_count: int
    run_count: int


class AgentVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    base_prompt: str
    model: str | None
    instructions: str
    skills: list[AgentSkillOut]
    tools: list[AgentToolOut]


class AgentSummary(BaseModel):
    id: int
    name: str
    description: str
    version: int
    # The current version's id, which a workflow node pins to run this agent.
    version_id: int
    skill_count: int
    tool_count: int
    run_count: int
    updated_at: datetime


class AgentOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: AgentVersionOut


class RunSummary(BaseModel):
    id: int
    agent_id: int
    agent_version_id: int
    agent_version: int
    title: str | None
    message_count: int
    # The trigger that started the run (e.g. a schedule), if one did and it still exists.
    trigger_id: int | None
    # How its latest turn is going or went, and when it began and ended; the model's tool calls
    # and tokens over all its turns. All null for a run from before these were kept.
    status: AgentRunStatus | None
    started_at: datetime | None
    finished_at: datetime | None
    tool_call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime
    updated_at: datetime


class RunOut(RunSummary):
    # AI SDK `UIMessage`s, for rendering the conversation with the same components as a live chat.
    ui_messages: list[dict[str, Any]]
    # The raw LangChain history: messages with their tool calls, results and usage.
    messages: list[dict[str, Any]]
    # The pinned version's instructions: the system prompt of every model call in the run.
    instructions: str


def get_owned_agent(db: Session, user_id: int, agent_id: int, *, deleted_ok: bool = False) -> Agent:
    agent = db.get(Agent, agent_id)
    if (
        agent is None
        or agent.owner_id != user_id
        or (agent.deleted_at is not None and not deleted_ok)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return agent


def lock_live_agent(db: Session, agent: Agent, *, read: bool = False) -> None:
    """Row-lock the agent until commit (see `lock_live`); a 404 if it was deleted meanwhile."""
    if not lock_live(db, Agent, [agent.id], read=read):
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def get_agent_version(db: Session, agent: Agent, number: int) -> AgentVersion:
    version = db.scalar(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id, AgentVersion.version == number
        )
    )
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return version


def get_owned_run(db: Session, user_id: int, agent_id: int, run_id: int) -> AgentRun:
    agent = get_owned_agent(db, user_id, agent_id)
    run = db.get(AgentRun, run_id)
    if run is None or run.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def resolve_skill_versions(db: Session, user_id: int, ids: list[int]) -> list[SkillVersion]:
    """The requested versions of the user's live skills, in request order."""
    found = {
        v.id: v
        for v in db.scalars(
            select(SkillVersion)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(SkillVersion.id.in_(ids), Skill.owner_id == user_id, Skill.deleted_at.is_(None))
        )
    }
    versions = [found[i] for i in ids if i in found]
    if len(versions) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown skill version")
    if len({v.skill_id for v in versions}) != len(versions):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Each skill can be attached only once"
        )
    # Lock the skills until commit and re-check they're live, so a concurrent delete either
    # waits for this save (and then sees the usage) or wins and we reject the attach.
    skill_ids = {v.skill_id for v in versions}
    if lock_live_skills(db, skill_ids) != skill_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown skill version")
    return versions


def resolve_tools(
    tools: list[AgentToolIn], current: AgentVersion | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """The requested tools as (key, settings) pairs, in request order.

    Settings sent back exactly as `current` stores them are kept as stored, so a later change to
    the tool's settings model doesn't turn a rename into a new version. They're still checked
    the way a run loads them, so a version that couldn't run isn't saved. Other settings are new
    and validated strictly.
    """
    if len({t.key for t in tools}) != len(tools):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Each tool can be added only once"
        )
    stored = {t.tool_key: t.config for t in current.tools} if current else {}
    resolved = []
    try:
        for t in tools:
            if t.key in stored and stored[t.key] == t.config:
                check_stored_tool(t.key, t.config)
                resolved.append((t.key, t.config))
            else:
                resolved.append((t.key, validate_tool(t.key, t.config)))
    except ToolError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return resolved


def tool_out(tool: AgentVersionTool) -> AgentToolOut:
    spec = TOOLS.get(tool.tool_key)
    return AgentToolOut(
        key=tool.tool_key,
        name=spec.name if spec else tool.tool_key,
        description=spec.description if spec else "",
        config=tool.config,
        available=spec is not None,
    )


def version_out(version: AgentVersion) -> AgentVersionOut:
    skills = []
    for sv in version.skills:
        skill = sv.skill
        latest = skill.current_version if skill.deleted_at is None else None
        skills.append(
            AgentSkillOut(
                skill_id=skill.id,
                skill_version_id=sv.id,
                name=sv.name,
                description=sv.description,
                version=sv.version,
                latest_version=latest.version if latest else None,
                latest_version_id=latest.id if latest else None,
            )
        )
    return AgentVersionOut(
        id=version.id,
        version=version.version,
        note=version.note,
        created_at=version.created_at,
        base_prompt=version.base_prompt,
        model=version.model,
        instructions=version.instructions,
        skills=skills,
        tools=[tool_out(t) for t in version.tools],
    )


def agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        current_version=version_out(agent.current_version),
    )


def skill_count(version_id) -> Any:
    return (
        select(func.count())
        .select_from(agent_version_skills)
        .where(agent_version_skills.c.agent_version_id == version_id)
        .scalar_subquery()
    )


def tool_count(version_id) -> Any:
    return (
        select(func.count())
        .select_from(AgentVersionTool)
        .where(AgentVersionTool.agent_version_id == version_id)
        .scalar_subquery()
    )


# --- Agents -------------------------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(user: CurrentUser, db: DB):
    run_count = (
        select(func.count(AgentRun.id)).where(AgentRun.agent_id == Agent.id).scalar_subquery()
    )
    rows = db.execute(
        select(
            Agent,
            AgentVersion.version,
            AgentVersion.id,
            skill_count(AgentVersion.id),
            tool_count(AgentVersion.id),
            run_count,
        )
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .where(Agent.owner_id == user.id, Agent.deleted_at.is_(None))
        .order_by(Agent.name)
    ).all()
    return [
        AgentSummary(
            id=a.id,
            name=a.name,
            description=a.description,
            version=version,
            version_id=version_id,
            skill_count=skills,
            tool_count=tools,
            run_count=runs,
            updated_at=a.updated_at,
        )
        for a, version, version_id, skills, tools, runs in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: CurrentUser, db: DB):
    tools = resolve_tools(body.tools)
    skills = resolve_skill_versions(db, user.id, body.skill_version_ids)
    agent = Agent(owner_id=user.id, name=body.name, description=body.description)
    db.add(agent)
    with unique_name(db, "an agent", AGENT_NAME_INDEX):
        save_agent_version(
            db,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            tools=tools,
            note=body.note,
            user_id=user.id,
        )
        db.commit()
    return agent_out(agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: DbId, user: CurrentUser, db: DB):
    return agent_out(get_owned_agent(db, user.id, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: DbId, body: AgentIn, user: CurrentUser, db: DB):
    """Rename/describe the agent; a changed prompt, skill set or tool set creates a new version."""
    agent = get_owned_agent(db, user.id, agent_id)
    tools = resolve_tools(body.tools, agent.current_version)
    skills = resolve_skill_versions(db, user.id, body.skill_version_ids)
    lock_live_agent(db, agent)
    agent.name = body.name
    agent.description = body.description
    with unique_name(db, "an agent", AGENT_NAME_INDEX):
        save_agent_version(
            db,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            tools=tools,
            note=body.note,
            user_id=user.id,
            model=agent.current_version.model,
        )
        db.commit()
    return agent_out(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: DbId, user: CurrentUser, db: DB, background: BackgroundTasks) -> None:
    agent = get_owned_agent(db, user.id, agent_id)
    # Hold the row lock across the usage check and the delete; workflow saves that run the agent
    # take it too (`aios.api.workflows.lock_graph`), and so does starting a run.
    lock_live_agent(db, agent)
    used_by = workflows_using(
        db,
        WorkflowNode.agent_version_id.in_(
            select(AgentVersion.id).where(AgentVersion.agent_id == agent.id)
        ),
    )
    if used_by:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Remove this agent from these workflows first: {', '.join(used_by)}.",
        )
    # Soft delete: older workflow versions may still pin its versions, so the agent and its
    # versions stay. Its runs are the user's conversations with it, and go now.
    run_ids = list(db.scalars(select(AgentRun.id).where(AgentRun.agent_id == agent.id)))
    sandboxes = run_sandboxes(db, run_ids)
    db.execute(delete(AgentRun).where(AgentRun.agent_id == agent.id))  # and their sessions
    delete_triggers(db, Trigger.agent_id == agent.id)
    agent.deleted_at = func.now()
    db.commit()
    # Their checkpoints aren't tied to the runs, so they don't cascade.
    background.add_task(checkpoints.delete_threads, run_ids)
    # Their session rows went with the runs; the sandboxes would run on until they time out.
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


@router.get("/agents/{agent_id}/versions", response_model=list[AgentVersionSummary])
def list_agent_versions(agent_id: DbId, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    run_count = (
        select(func.count(AgentRun.id))
        .where(AgentRun.agent_version_id == AgentVersion.id)
        .scalar_subquery()
    )
    rows = db.execute(
        select(AgentVersion, skill_count(AgentVersion.id), tool_count(AgentVersion.id), run_count)
        .where(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.version.desc())
    ).all()
    return [
        AgentVersionSummary(
            id=v.id,
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            skill_count=skills,
            tool_count=tools,
            run_count=runs,
        )
        for v, skills, tools, runs in rows
    ]


@router.get("/agents/{agent_id}/versions/{number}", response_model=AgentVersionOut)
def get_agent_version_detail(agent_id: DbId, number: DbId, user: CurrentUser, db: DB):
    # Also once the agent is deleted: workflow versions, and their past runs, still pin its
    # versions, as they do a deleted workflow's.
    agent = get_owned_agent(db, user.id, agent_id, deleted_ok=True)
    return version_out(get_agent_version(db, agent, number))


@router.post("/agents/{agent_id}/versions/{number}/restore", response_model=AgentOut)
def restore_agent_version(agent_id: DbId, number: DbId, user: CurrentUser, db: DB):
    """Make an old version's config current again, as a new version (numbers only go up)."""
    agent = get_owned_agent(db, user.id, agent_id)
    old = get_agent_version(db, agent, number)
    # Kept as stored, like an unchanged save, but only if a run could still load them.
    build_version_tools(old, "That version can't be restored")
    tools = [(t.tool_key, t.config) for t in old.tools]
    skill_ids = {sv.skill_id for sv in old.skills}
    if lock_live_skills(db, skill_ids) != skill_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That version uses a skill that has since been deleted."
        )
    lock_live_agent(db, agent)
    save_agent_version(
        db,
        agent,
        base_prompt=old.base_prompt,
        skills=list(old.skills),
        tools=tools,
        model=old.model,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    db.commit()
    return agent_out(agent)


# --- Runs ---------------------------------------------------------------------------------------


def run_summary_query():
    return select(
        AgentRun.id,
        AgentRun.agent_id,
        AgentRun.agent_version_id,
        AgentVersion.version.label("agent_version"),
        AgentRun.title,
        AgentRun.message_count,
        AgentRun.trigger_id,
        AgentRun.status,
        AgentRun.started_at,
        AgentRun.finished_at,
        AgentRun.tool_call_count,
        AgentRun.input_tokens,
        AgentRun.output_tokens,
        AgentRun.created_at,
        AgentRun.updated_at,
    ).join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)


def build_version_tools(
    version: AgentVersion, refusal: str = "This agent version can't be run"
) -> list[AgentTool]:
    """The version's tools, built from their stored settings; a 409 if one can't be."""
    try:
        return version_tools(version)
    except ToolError as exc:
        # E.g. the tool has since left the library, or its settings model now rejects a value.
        raise HTTPException(status.HTTP_409_CONFLICT, f"{refusal}: {exc}") from exc


@router.get("/agents/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: DbId, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    rows = db.execute(
        run_summary_query()
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return [RunSummary.model_validate(row, from_attributes=True) for row in rows]


def new_agent_run(db: Session, agent: Agent, *, trigger_id: int | None = None) -> AgentRun:
    """Add (and flush, not commit) a run of the agent's current version.

    Raises `HTTPException` if it can't run: 404 if the agent has been deleted, 409 if its
    version can't be built. The caller commits; the share lock taken here, held until then,
    keeps a concurrent delete from missing the run and leaving it behind. Also how a schedule
    starts one (`aios.scheduler`), passing its `trigger_id`.
    """
    lock_live_agent(db, agent, read=True)
    # Checked now, so a version that can't be run doesn't leave behind a run that can't be used.
    build_version_tools(agent.current_version)
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(agent=agent, agent_version_id=agent.current_version_id, trigger_id=trigger_id)
    db.add(run)
    db.flush()
    return run


@router.post(
    "/agents/{agent_id}/runs", response_model=RunSummary, status_code=status.HTTP_201_CREATED
)
def create_run(agent_id: DbId, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    run = new_agent_run(db, agent)
    db.commit()
    row = db.execute(run_summary_query().where(AgentRun.id == run.id)).one()
    return RunSummary.model_validate(row, from_attributes=True)


@router.get("/agents/{agent_id}/runs/{run_id}", response_model=RunOut)
def get_run(agent_id: DbId, run_id: DbId, user: CurrentUser, db: DB):
    run = get_owned_run(db, user.id, agent_id, run_id)
    row = db.execute(run_summary_query().where(AgentRun.id == run.id)).one()
    history = checkpoints.history(run.id)
    return RunOut(
        **RunSummary.model_validate(row, from_attributes=True).model_dump(),
        ui_messages=ui_stream.to_ui_messages(history),
        messages=checkpoints.messages_out(history),
        instructions=run.agent_version.instructions,
    )


@router.delete("/agents/{agent_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(
    agent_id: DbId, run_id: DbId, user: CurrentUser, db: DB, background: BackgroundTasks
) -> None:
    run = get_owned_run(db, user.id, agent_id, run_id)
    sandboxes = run_sandboxes(db, [run.id])
    db.delete(run)
    db.commit()
    background.add_task(checkpoints.delete_threads, [run_id])
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


def trace_metadata(run: AgentRun, user_id: int) -> dict[str, Any]:
    """Attached to the run's trace (Langfuse), e.g. for filtering and experiments."""
    version = run.agent_version
    return tracing.trace_metadata(
        session_id=checkpoints.thread_id(run.id),
        user_id=user_id,
        tags=[f"agent:{run.agent.name}", f"agent-version:{version.version}"],
        agent_run_id=run.id,
        **tracing.agent_version_metadata(version),
    )


@dataclass(frozen=True)
class ChatContext:
    """Everything a chat turn needs from the database, as plain values."""

    model: str | None
    instructions: str
    metadata: dict[str, Any]
    # The version's tools, built from their stored settings.
    tools: list[AgentTool] = field(default_factory=list)
    # The `run_python` tool's allowed domains, if the version has the tool and the run's
    # sandbox is worth pre-warming for this turn (see `worth_prewarming`).
    sandbox_domains: list[str] | None = None
    # The data store the turn's tools use: the user's.
    data_store_id: uuid.UUID | None = None


def load_chat_context(db: Session, user_id: int, agent_id: int, run_id: int) -> ChatContext:
    run = get_owned_run(db, user_id, agent_id, run_id)
    version = run.agent_version
    tools = build_version_tools(version)
    # Parsed as the tool itself is built (`build_tool`), so a pre-warmed sandbox's proxy token
    # carries the domains the tool expects.
    sandbox_domains = next(
        (
            stored_settings(t.tool_key, t.config).allowed_domains
            for t in version.tools
            if t.tool_key == "run_python"
        ),
        None,
    )
    if sandbox_domains is not None and not worth_prewarming(
        db, run.id, first_turn=run.message_count == 0
    ):
        sandbox_domains = None
    # On this session, rather than one of its own alongside it; committed in case it's created.
    data_store_id = data_store_for(user_id, db)
    db.commit()
    return ChatContext(
        model=version.model,
        instructions=version.instructions,
        metadata=trace_metadata(run, user_id),
        tools=tools,
        sandbox_domains=sandbox_domains,
        data_store_id=data_store_id,
    )


def _start_turn(run_id: int, prompt: str, *, heartbeat: bool = False) -> None:
    """Mark the run running as a turn begins, and title it after its first message.

    With `heartbeat`, the turn runs outside a request and its process will keep the run's
    heartbeat (`aios.firing`); without, any earlier one is cleared, so a request's turn isn't
    failed as stalled."""
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                title=func.coalesce(AgentRun.title, run_title(prompt)),
                status=AgentRunStatus.running,
                started_at=func.now(),
                finished_at=None,
                heartbeat_at=func.now() if heartbeat else None,
            )
        )
        db.commit()


def tool_call_count(response: AIMessage) -> int:
    """The tool calls in a model response: the agent's, and the provider's own (e.g. web
    search), which are content blocks of the response (as the run page counts them)."""
    blocks = response.content if isinstance(response.content, list) else []
    server = sum(isinstance(b, dict) and b.get("type") == "server_tool_use" for b in blocks)
    return len(response.tool_calls) + server


def _count_step(run_id: int, response: AIMessage) -> None:
    """Add a model step to the run's counters: its tool calls, and what the call cost."""
    usage = response.usage_metadata or {}
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                tool_call_count=func.coalesce(AgentRun.tool_call_count, 0)
                + tool_call_count(response),
                input_tokens=func.coalesce(AgentRun.input_tokens, 0) + usage.get("input_tokens", 0),
                output_tokens=func.coalesce(AgentRun.output_tokens, 0)
                + usage.get("output_tokens", 0),
            )
        )
        db.commit()


def _end_turn(run_id: int, *, failed: bool, message_count: int | None = None) -> None:
    """Record how the turn ended; `message_count` is left as it was if the turn never started."""
    values: dict[str, Any] = {
        "status": AgentRunStatus.failed if failed else AgentRunStatus.succeeded,
        "finished_at": func.now(),
        "updated_at": func.now(),
    }
    if message_count is not None:
        values["message_count"] = message_count
    with SessionLocal() as db:
        # A no-op if the run was deleted mid-turn.
        db.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
        db.commit()


def model_responses(mode: str, data: Any) -> list[AIMessage]:
    """The model's finished responses in one item of the agent's stream: each is a step."""
    if mode != "updates":
        return []
    return [
        message
        for node, update in data.items()
        if node == "model"
        for message in (update or {}).get("messages", [])
        if isinstance(message, AIMessage)
    ]


async def _keep_unfinished_turn(agent, config, partial: AIMessage | None) -> None:
    """Record a turn that failed or was stopped, so the next turn sees what the user saw.

    The user's message is already in the thread (it's checkpointed before the model is called).
    What's added: the reply's text so far, and, if it stopped while tools were running, a result
    for each call still waiting for one (a call without a result can't be sent to the model).
    """
    state = await agent.aget_state(config)
    messages = state.values.get("messages", [])
    last = messages[-1] if messages else None
    # The model's last message, and the results of its tool calls so far.
    i = len(messages)
    while i and isinstance(messages[i - 1], ToolMessage):
        i -= 1
    calls, results = (messages[i - 1] if i else None), messages[i:]
    answered = {m.tool_call_id for m in results}
    stopped = [
        ToolMessage(
            "The turn ended before this tool call finished.",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
        for call in (calls.tool_calls if isinstance(calls, AIMessage) else [])
        if call["id"] not in answered
    ]
    if isinstance(calls, AIMessage) and calls.tool_calls:
        # Written even if every call has a result: the model's message and the results can be
        # only pending writes of the step the turn ended in, which an update's checkpoint doesn't
        # carry over. By id, so those already checkpointed aren't repeated.
        await agent.aupdate_state(
            config, {"messages": [calls, *results, *stopped]}, as_node="tools"
        )
    # A reply to the user's message or to tool results: its message isn't in the state until the
    # model has finished it.
    if partial is not None and isinstance(last, HumanMessage | ToolMessage):
        await agent.aupdate_state(config, {"messages": [partial]}, as_node="model")


async def _record_turn(agent, config, run_id: int, *, finished: bool, partial) -> None:
    try:
        if not finished:
            await _keep_unfinished_turn(agent, config, partial)
        state = await agent.aget_state(config)
        await run_in_threadpool(
            _end_turn,
            run_id,
            failed=not finished,
            message_count=len(state.values.get("messages", [])),
        )
    except Exception:
        log.exception("couldn't record the end of a turn of run %s", run_id)


# Pre-warming tasks, kept so they aren't garbage-collected before they finish.
_prewarming: set[asyncio.Task] = set()


def prewarm_sandbox(run_id: int, allowed_domains: list[str]) -> None:
    """Starts the run's sandbox in the background, so it's ready by the model's first call."""

    async def ensure() -> None:
        try:
            await manager.ensure(run_id=run_id, allowed_domains=allowed_domains)
        except SandboxNotConfigured:
            pass
        except Exception:
            # The tool call will try again, and report a failure to the model.
            log.exception("couldn't pre-warm the sandbox for run %s", run_id)

    task = asyncio.create_task(ensure())
    _prewarming.add(task)
    task.add_done_callback(_prewarming.discard)


async def turn_stream(
    ctx: ChatContext, user_id: int, run_id: int, prompt: HumanMessage
) -> AsyncIterator[ui_stream.Chunk]:
    """One turn of a run, as AI SDK UI message chunks: the user's message is checkpointed before
    the model is called, and whatever the reply got to is kept however the turn ends (it
    completes, fails, or the consumer stops it). A failure is reported as an `error` chunk.

    Shared by the chat endpoint and the scheduler (`aios.scheduler`)."""
    config = {
        **checkpoints.thread_config(run_id),
        "callbacks": tracing.callbacks(),
        "run_name": "agent-run",
        "metadata": ctx.metadata,
    }
    ui = ui_stream.UIStream()
    for chunk in ui.start():
        yield chunk
    if ctx.sandbox_domains is not None:
        prewarm_sandbox(run_id, ctx.sandbox_domains)
    try:
        # Waits for any other turn of this run to finish (see `checkpoints.turn`).
        async with checkpoints.turn(run_id) as saver:
            agent = build_agent(
                model=ctx.model,
                instructions=ctx.instructions,
                tools=ctx.tools,
                checkpointer=saver,
            )
            finished = False
            # Closed however the turn ends, which cancels the graph's running tasks (e.g. a
            # tool call) before the turn is recorded: a stopped turn's response can be
            # dropped while this waits at a `yield`, which doesn't close the stream itself.
            events = agent.astream(
                {"messages": [prompt]},
                config,
                context=ChatDeps(user_id=user_id, run_id=run_id, data_store_id=ctx.data_store_id),
                stream_mode=["messages", "updates"],
                # Checkpoint each step before the next starts, so the user's message is
                # saved before the model is called.
                durability="sync",
            )
            try:
                async with aclosing(events):
                    async for mode, data in events:
                        for chunk in ui.feed(mode, data):
                            yield chunk
                        # One small update per model step, so the run's counters are live.
                        for response in model_responses(mode, data):
                            await run_in_threadpool(_count_step, run_id, response)
                finished = True
            except Exception as exc:
                log.exception("agent run %s failed", run_id)
                for chunk in ui.error(exc):
                    yield chunk
            finally:
                # Also when the client stops the turn, which cancels this task.
                with CancelScope(shield=True):
                    await _record_turn(
                        agent, config, run_id, finished=finished, partial=ui.partial_reply()
                    )
    except Exception as exc:
        # The turn couldn't start, e.g. the database or the model's configuration.
        log.exception("agent run %s couldn't start a turn", run_id)
        with CancelScope(shield=True):
            try:
                await run_in_threadpool(_end_turn, run_id, failed=True)
            except Exception:
                log.exception("couldn't record the end of a turn of run %s", run_id)
        for chunk in ui.error(exc):
            yield chunk
    for chunk in ui.finish():
        yield chunk


@router.post("/agents/{agent_id}/runs/{run_id}/chat")
async def run_chat(agent_id: DbId, run_id: DbId, body: dict, user: CurrentUser, db: DB):
    """Continue a run. The client sends only its new message; history comes from the run's
    LangGraph thread.

    The user's message is checkpointed before the model is called, and whatever the reply got
    to is kept when the turn ends, whether it completes, fails, or is stopped (the client
    disconnecting), so the stored history, which is all the next turn sees, always matches
    what the user saw.
    """
    request = ui_stream.parse_request(body)
    # Sync SQLAlchemy calls go to a worker thread so they don't block the event loop. The `db`
    # session is function-scoped (see `aios.db.DB`), so it's closed and its connection returned
    # to the pool before the response streams; the saves use sessions of their own.
    ctx = await run_in_threadpool(load_chat_context, db, user.id, agent_id, run_id)
    message = request.messages[-1]
    if message.role != "user":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Expected a user message")
    prompt = ui_stream.human_message(message)
    await run_in_threadpool(_start_turn, run_id, prompt.text)

    return ui_stream.streaming_response(turn_stream(ctx, user.id, run_id, prompt))
