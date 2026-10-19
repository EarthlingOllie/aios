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
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, true, update
from sqlalchemy.orm import Session, object_session

from aios import checkpoints, dependents, tracing, ui_stream
from aios.agent import ChatDeps
from aios.api.provenance import Provenance, UserOut, made_by, people, provenance, started_by
from aios.api.schema_changes import SchemaChangeOut, run_requests, waiting_on
from aios.api.skills import lock_live, lock_live_skills, unique_name
from aios.api.triggers import delete_triggers, disable_triggers
from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import data_store_of, history, schema_changes
from aios.db import DB, DbId, SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentRunStatus,
    AgentVersion,
    AgentVersionTool,
    Chat,
    ChatInvocation,
    Organisation,
    SchemaChangeRequest,
    SchemaChangeStatus,
    Skill,
    SkillVersion,
    Trigger,
    Workflow,
    WorkflowRunStep,
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
from aios.workflows import cancel_runs

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

AGENT_NAME_INDEX = "uq_agents_organisation_id_name_live"


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
    # The member who saved it.
    created_by: UserOut | None
    skill_count: int
    tool_count: int
    run_count: int


class AgentVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    # The member who saved it.
    created_by: UserOut | None
    base_prompt: str
    model: str | None
    instructions: str
    skills: list[AgentSkillOut]
    tools: list[AgentToolOut]


class AgentSummary(Provenance):
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


class AgentOut(Provenance):
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
    # The chat a mention in it started it from, while the chat exists.
    chat_id: int | None
    chat_name: str | None
    # The member who started it: by hand, by mentioning the agent in a chat, or by starting the
    # workflow run it's a node's conversation in. Null for a schedule's run (`trigger_id` says
    # which, while it exists), and once that user is deleted.
    created_by: UserOut | None
    created_at: datetime
    updated_at: datetime


class RunOut(RunSummary):
    # The agent's name, so the page needn't read the agent as well.
    agent_name: str
    # AI SDK `UIMessage`s, for rendering the conversation with the same components as a live chat.
    ui_messages: list[dict[str, Any]]
    # The raw LangChain history: messages with their tool calls, results and usage.
    messages: list[dict[str, Any]]
    # The pinned version's instructions: the system prompt of every model call in the run.
    instructions: str
    # The schema change requests the run made (oldest first), each at its tool call.
    schema_change_requests: list[SchemaChangeOut]
    # Whether it waits on one of them, and takes no new messages until it's resumed.
    waiting: bool


def get_org_agent(
    db: Session, org: Organisation, agent_id: int, *, deleted_ok: bool = False
) -> Agent:
    """The organisation's agent (live, unless `deleted_ok`); a 404 otherwise, as if it didn't
    exist."""
    agent = db.get(Agent, agent_id)
    if (
        agent is None
        or agent.organisation_id != org.id
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


def get_org_run(db: Session, org: Organisation, agent_id: int, run_id: int) -> AgentRun:
    """A run of the organisation's live agent; a 404 otherwise, as if it didn't exist. Every
    member reads, continues and deletes every run of the organisation's, whoever started it
    and however (by hand, from a chat, by a schedule)."""
    run = get_live_run(db, agent_id, run_id)
    if run.agent.organisation_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def get_live_run(db: Session, agent_id: int, run_id: int) -> AgentRun:
    """A run of the live agent, whoever's it is; a 404 otherwise."""
    agent = db.get(Agent, agent_id)
    run = db.get(AgentRun, run_id)
    if agent is None or agent.deleted_at is not None or run is None or run.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def resolve_skill_versions(db: Session, org: Organisation, ids: list[int]) -> list[SkillVersion]:
    """The requested versions of the organisation's live skills, in request order."""
    found = {
        v.id: v
        for v in db.scalars(
            select(SkillVersion)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(
                SkillVersion.id.in_(ids),
                Skill.organisation_id == org.id,
                Skill.deleted_at.is_(None),
            )
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


def version_out(version: AgentVersion, known: dict[int, UserOut] | None = None) -> AgentVersionOut:
    """The version, with who saved it from `known` (`provenance.people`), else read now."""
    if known is None:
        known = people(object_session(version), [version.created_by])
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
        created_by=known.get(version.created_by),
        base_prompt=version.base_prompt,
        model=version.model,
        instructions=version.instructions,
        skills=skills,
        tools=[tool_out(t) for t in version.tools],
    )


def agent_out(db: Session, agent: Agent) -> AgentOut:
    version = agent.current_version
    known = people(db, [agent.created_by, agent.updated_by, version.created_by])
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        current_version=version_out(version, known),
        **provenance(db, agent, known),
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
def list_agents(org: CurrentOrganisation, db: DB):
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
        .where(Agent.organisation_id == org.id, Agent.deleted_at.is_(None))
        .order_by(Agent.name)
    ).all()
    known = people(db, [i for a, *_ in rows for i in (a.created_by, a.updated_by)])
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
            **provenance(db, a, known),
        )
        for a, version, version_id, skills, tools, runs in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    tools = resolve_tools(body.tools)
    skills = resolve_skill_versions(db, org, body.skill_version_ids)
    agent = Agent(**made_by(org.id, user.id), name=body.name, description=body.description)
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
    return agent_out(db, agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: DbId, org: CurrentOrganisation, db: DB):
    return agent_out(db, get_org_agent(db, org, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: DbId, body: AgentIn, user: CurrentUser, org: CurrentOrganisation, db: DB
):
    """Rename/describe the agent; a changed prompt, skill set or tool set creates a new version."""
    agent = get_org_agent(db, org, agent_id)
    tools = resolve_tools(body.tools, agent.current_version)
    skills = resolve_skill_versions(db, org, body.skill_version_ids)
    lock_live_agent(db, agent)
    agent.name = body.name
    agent.description = body.description
    agent.updated_by = user.id
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
    return agent_out(db, agent)


@router.get("/agents/{agent_id}/dependents", response_model=dependents.Dependents)
def get_agent_dependents(agent_id: DbId, org: CurrentOrganisation, db: DB):
    """What depends on the agent: what deleting it would cut off, and so has to be resolved
    first (see `delete_agent`)."""
    agent = get_org_agent(db, org, agent_id)
    dependents.fail_stopped_runs(db, agent)
    return dependents.find(db, agent)


class AgentDeleteIn(BaseModel):
    """How to resolve what depends on the agent (`GET /api/agents/{id}/dependents`), as the user
    chose. Runs are named one by one, so one that starts after the user looked isn't cancelled
    unseen: the delete is refused, listing it."""

    # Chat runs to stop (they're deleted with the agent, and their requests cancelled).
    cancel_agent_runs: list[DbId] = []
    # Workflow runs to stop: they fail, and their requests are cancelled.
    cancel_workflow_runs: list[DbId] = []
    # Workflows to deactivate: their schedules are turned off. Any other workflow that runs the
    # agent must be edited to stop running it first.
    deactivate_workflows: list[DbId] = []


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(
    agent_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    background: BackgroundTasks,
    body: AgentDeleteIn | None = None,
) -> None:
    """Delete the agent once what depends on it is resolved: the runs still going are named in
    `cancel_*`, and each workflow that runs it is deactivated or has been edited. Otherwise a
    409 whose detail has a `message` and the `dependents`. All in one transaction: refused,
    nothing changes."""
    body = body or AgentDeleteIn()
    agent = get_org_agent(db, org, agent_id)
    dependents.fail_stopped_runs(db, agent)  # commits; before anything is locked
    # Hold the row lock across the check and the delete; workflow saves that run the agent take
    # it too (`aios.api.workflows.lock_graph`), and so does starting a chat run.
    lock_live_agent(db, agent)
    # Starting a run of a workflow share-locks the workflow (`new_workflow_run`), so with the
    # workflows that run the agent locked, a run being started is waited for and then seen,
    # and one started after waits for this, and then can't (it would run a deleted agent).
    # Until none is found that isn't locked: a save waited for meanwhile may nest one in another.
    locked: set[int] = set()
    while unlocked := dependents.workflow_ids(db, agent) - locked:
        lock_live(db, Workflow, unlocked)
        locked |= unlocked
    found = dependents.find(db, agent)
    refusal = dependents.unresolved(
        found,
        cancel_agent_runs=set(body.cancel_agent_runs),
        cancel_workflow_runs=set(body.cancel_workflow_runs),
        deactivate_workflows=set(body.deactivate_workflows),
    )
    if refusal is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": refusal, "dependents": found.model_dump(mode="json")},
        )
    # Before the agent runs go: found by their steps, their requests are cancelled.
    cancel_runs(
        db,
        [r.id for r in found.workflow_runs],
        f"Cancelled: the agent {agent.name} was deleted.",
    )
    disable_triggers(db, Trigger.workflow_id.in_([w.id for w in found.workflows]), user.id)
    # Soft delete: older workflow versions may still pin its versions, so the agent and its
    # versions stay. Its runs are the user's conversations with it, and go now.
    run_ids = list(db.scalars(select(AgentRun.id).where(AgentRun.agent_id == agent.id)))
    sandboxes = run_sandboxes(db, run_ids)
    cancel_chat_requests(db, run_ids)
    history.enqueue_runs(db, AgentRun.agent_id == agent.id, deleting=True)  # their copies go too
    db.execute(delete(AgentRun).where(AgentRun.agent_id == agent.id))  # and their sessions
    delete_triggers(db, Trigger.agent_id == agent.id)
    agent.deleted_at = func.now()
    agent.deleted_by = user.id
    db.commit()
    # Turns still underway (a chat's, a schedule's, a cancelled workflow run's node) stop now,
    # rather than carrying on with tool calls for runs that are gone.
    checkpoints.stop_turns(db, run_ids)
    db.commit()
    # Their checkpoints aren't tied to the runs, so they don't cascade.
    background.add_task(checkpoints.delete_threads, run_ids)
    # Their session rows went with the runs; the sandboxes would run on until they time out.
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


@router.get("/agents/{agent_id}/versions", response_model=list[AgentVersionSummary])
def list_agent_versions(agent_id: DbId, org: CurrentOrganisation, db: DB):
    agent = get_org_agent(db, org, agent_id)
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
    known = people(db, [v.created_by for v, *_ in rows])
    return [
        AgentVersionSummary(
            id=v.id,
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by=known.get(v.created_by),
            skill_count=skills,
            tool_count=tools,
            run_count=runs,
        )
        for v, skills, tools, runs in rows
    ]


@router.get("/agents/{agent_id}/versions/{number}", response_model=AgentVersionOut)
def get_agent_version_detail(agent_id: DbId, number: DbId, org: CurrentOrganisation, db: DB):
    # Also once the agent is deleted: workflow versions, and their past runs, still pin its
    # versions, as they do a deleted workflow's.
    agent = get_org_agent(db, org, agent_id, deleted_ok=True)
    return version_out(get_agent_version(db, agent, number))


@router.post("/agents/{agent_id}/versions/{number}/restore", response_model=AgentOut)
def restore_agent_version(
    agent_id: DbId, number: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB
):
    """Make an old version's config current again, as a new version (numbers only go up)."""
    agent = get_org_agent(db, org, agent_id)
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
    agent.updated_by = user.id
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
    return agent_out(db, agent)


# --- Runs ---------------------------------------------------------------------------------------


def run_summary_query():
    return (
        select(
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
            Chat.id.label("chat_id"),
            Chat.name.label("chat_name"),
            # As the user, by `run_summary` and `list_runs` (`provenance.started_by`).
            AgentRun.created_by.label("created_by_id"),
            AgentRun.created_at,
            AgentRun.updated_at,
        )
        .join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
        .outerjoin(ChatInvocation, ChatInvocation.agent_run_id == AgentRun.id)
        .outerjoin(Chat, Chat.id == ChatInvocation.chat_id)
    )


def run_summary(db: Session, run_id: int) -> RunSummary:
    row = db.execute(run_summary_query().where(AgentRun.id == run_id)).one()
    return started_by(db, [row], RunSummary)[0]


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
def list_runs(agent_id: DbId, org: CurrentOrganisation, db: DB):
    agent = get_org_agent(db, org, agent_id)
    rows = db.execute(
        run_summary_query()
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return started_by(db, rows, RunSummary)


def new_agent_run(
    db: Session, agent: Agent, *, created_by: int | None, trigger_id: int | None = None
) -> AgentRun:
    """Add (and flush, not commit) a run of the agent's current version, started by the member
    `created_by`: who asked for it by hand or in a chat. It runs for the agent's organisation,
    not for them (`load_chat_context`).

    Raises `HTTPException` if it can't run: 404 if the agent has been deleted, 409 if its
    version can't be built. The caller commits; the share lock taken here, held until then,
    keeps a concurrent delete from missing the run and leaving it behind. Also how a schedule
    starts one (`aios.scheduler`), passing its `trigger_id` and no member.
    """
    lock_live_agent(db, agent, read=True)
    # Checked now, so a version that can't be run doesn't leave behind a run that can't be used.
    build_version_tools(agent.current_version)
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(
        agent=agent,
        agent_version_id=agent.current_version_id,
        trigger_id=trigger_id,
        created_by=created_by,
    )
    db.add(run)
    db.flush()
    return run


@router.post(
    "/agents/{agent_id}/runs", response_model=RunSummary, status_code=status.HTTP_201_CREATED
)
def create_run(agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    agent = get_org_agent(db, org, agent_id)
    run = new_agent_run(db, agent, created_by=user.id)
    db.commit()
    return run_summary(db, run.id)


@router.get("/agents/{agent_id}/runs/{run_id}", response_model=RunOut)
def get_run(agent_id: DbId, run_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    run = get_org_run(db, org, agent_id, run_id)
    summary = run_summary(db, run.id)
    messages = checkpoints.history(run.id)
    return RunOut(
        **summary.model_dump(),
        agent_name=run.agent.name,
        ui_messages=ui_stream.to_ui_messages(messages),
        messages=checkpoints.messages_out(messages),
        instructions=run.agent_version.instructions,
        schema_change_requests=run_requests(db, run.id, user.id),
        waiting=waiting_on(db, run.id),
    )


@router.delete("/agents/{agent_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(
    agent_id: DbId, run_id: DbId, org: CurrentOrganisation, db: DB, background: BackgroundTasks
) -> None:
    run = get_org_run(db, org, agent_id, run_id)
    sandboxes = run_sandboxes(db, [run.id])
    cancel_chat_requests(db, [run.id])
    # Its exported copy goes too (found through the run, so before it's deleted).
    history.enqueue_runs(db, AgentRun.id == run.id, deleting=True)
    db.delete(run)
    db.commit()
    background.add_task(checkpoints.delete_threads, [run_id])
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


def trace_metadata(run: AgentRun) -> dict[str, Any]:
    """Attached to the run's trace (Langfuse), e.g. for filtering and experiments: the agent's
    organisation, which the run executes for, and the member who started it."""
    version = run.agent_version
    return tracing.trace_metadata(
        session_id=checkpoints.thread_id(run.id),
        organisation_id=run.agent.organisation_id,
        created_by=run.created_by,
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
    # The data store the turn's tools use: the agent's organisation's.
    data_store_id: uuid.UUID | None = None
    # The member who started the run, which the turn's tools are told (`ChatDeps.user_id`);
    # None for a schedule's.
    created_by: int | None = None


def load_chat_context(
    db: Session, agent_id: int, run_id: int, *, org: Organisation | None = None
) -> ChatContext:
    """What a turn of the run needs. The turn executes for the agent's organisation, whoever
    asks for it: its store is the organisation's, and its trace and tools are told who started
    the run. `org` is the organisation of the request asking for the turn, which the run's agent
    must be in (a 404 otherwise); the scheduler, a chat's mention and a resumed run are let in
    by what started them, and pass none."""
    run = (
        get_live_run(db, agent_id, run_id)
        if org is None
        else get_org_run(db, org, agent_id, run_id)
    )
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
    # The agent's organisation's, on this session rather than one of its own alongside it;
    # committed in case it's created.
    data_store_id = data_store_of(db, Agent, agent_id)
    db.commit()
    return ChatContext(
        model=version.model,
        instructions=version.instructions,
        metadata=trace_metadata(run),
        tools=tools,
        sandbox_domains=sandbox_domains,
        data_store_id=data_store_id,
        created_by=run.created_by,
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
        # Every turn ends here (chats, schedules, workflow agent nodes), however it ended: with
        # the run's own update, it goes in the run history export's outbox. The outbox row
        # first, as deleting a run does, so the two never each hold the row the other wants.
        history.enqueue_runs(db, AgentRun.id == run_id)
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


async def _keep_unfinished_turn(agent, config, partial: AIMessage | None) -> list[str]:
    """Record a turn that failed or was stopped, so the next turn sees what the user saw.

    The user's message is already in the thread (it's checkpointed before the model is called).
    What's added: the reply's text so far, and, if it stopped while tools were running, a result
    for each call still waiting for one (a call without a result can't be sent to the model).
    Returns the ids of those calls.
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
    return [m.tool_call_id for m in stopped]


def _cancel_requests(run_id: int, tool_call_ids: list[str]) -> None:
    """Cancel the pending schema change requests these tool calls made: the model has been told
    the calls didn't finish, so nothing waits on them any more."""
    if not tool_call_ids:
        return
    with SessionLocal() as db:
        schema_changes.cancel(
            db,
            SchemaChangeRequest.requested_by_run == run_id,
            SchemaChangeRequest.tool_call_id.in_(tool_call_ids),
        )
        db.commit()


def cancel_chat_requests(db: Session, run_ids: list[int]) -> None:
    """Before these agent runs are deleted (their requests keep no link to them): cancel the
    pending requests their chats wait on, so none can be approved with nothing left to resume.
    A workflow node's run is left alone: its workflow run still waits on the request and resumes
    on its decision (`aios.workflows.workflow_run_waiting_on`), and ending or deleting that run
    closes it out (`aios.workflows.close_requests`). In the caller's transaction.

    The runs are locked first: a turn recording a request meanwhile holds a key share on its
    run until it commits, so this waits and then sees it; one that starts later fails to
    record it once the run has gone."""
    db.execute(select(AgentRun.id).where(AgentRun.id.in_(run_ids)).with_for_update())
    schema_changes.cancel(
        db,
        SchemaChangeRequest.requested_by_run.in_(run_ids),
        SchemaChangeRequest.requested_by_run.not_in(
            select(WorkflowRunStep.agent_run_id).where(WorkflowRunStep.agent_run_id.in_(run_ids))
        ),
    )


async def _record_turn(agent, config, run_id: int, *, finished: bool, partial) -> None:
    try:
        if not finished:
            stopped = await _keep_unfinished_turn(agent, config, partial)
            await run_in_threadpool(_cancel_requests, run_id, stopped)
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


# What a new message to a run waiting on a schema change request is told.
WAITING = (
    "This run is waiting for a decision on its schema change request: approve or reject it"
    " first, then carry on."
)


class RunWaiting(Exception):
    """A new message to a run that waits on a schema change request."""

    def __init__(self) -> None:
        super().__init__(WAITING)


@dataclass
class Resume:
    """A turn that resumes a run waiting on schema change requests, giving each waiting tool
    call its request's outcome, for those decided (`resume_run`). The turn fills in which
    requests it dealt with, once it has the run to itself."""

    # The decided requests (not yet resumed) it found: given to their calls, or with no call
    # waiting on them any more.
    dealt_with: list[uuid.UUID] | None = None


def _decided_requests(run_id: int) -> dict[str, dict[str, Any]]:
    """The run's decided requests it hasn't been given yet, with their outcomes, by id."""
    with SessionLocal() as db:
        requests = db.scalars(
            select(SchemaChangeRequest).where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.resumed_at.is_(None),
            )
        ).all()
        return {str(r.id): schema_changes.outcome(r) for r in requests if r.status.decided}


async def _resume_command(agent, config, run_id: int, resume: Resume) -> Command | None:
    """The command resuming the run's waiting tool calls whose requests are decided (by
    interrupt id, so others keep waiting); None if none are."""
    from aios.tools.schema_change import REQUEST_KEY

    state = await agent.aget_state(config)
    waiting = {
        i.id: i.value[REQUEST_KEY]
        for i in state.interrupts
        if isinstance(i.value, dict) and REQUEST_KEY in i.value
    }
    outcomes = await run_in_threadpool(_decided_requests, run_id)
    # Those decided after this aren't dealt with: their own resume gives them to the run.
    resume.dealt_with = [uuid.UUID(rid) for rid in outcomes]
    values = {iid: outcomes[rid] for iid, rid in waiting.items() if rid in outcomes}
    return Command(resume=values) if values else None


def _check_not_waiting(run_id: int) -> None:
    with SessionLocal() as db:
        if waiting_on(db, run_id):
            raise RunWaiting


async def turn_stream(
    ctx: ChatContext, run_id: int, turn: HumanMessage | Resume
) -> AsyncIterator[ui_stream.Chunk]:
    """One turn of a run, as AI SDK UI message chunks: the user's message is checkpointed before
    the model is called, and whatever the reply got to is kept however the turn ends (it
    completes, fails, or the consumer stops it). A failure is reported as an `error` chunk.

    `turn` is the user's message, or `Resume` to carry on a run that waits on schema change
    requests now they're decided; a message to a run still waiting is refused (an `error`
    chunk), checked once the turn has the run to itself.

    Shared by the chat endpoint, the scheduler (`aios.scheduler`) and `resume_run`."""
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
            if isinstance(turn, Resume):
                graph_input = await _resume_command(agent, config, run_id, turn)
            else:
                await run_in_threadpool(_check_not_waiting, run_id)
                graph_input = {"messages": [turn]}
            if graph_input is not None:
                # Closed here if the consumer stops the turn, so the turn is recorded while the
                # checkpointer's connection is still open.
                turn_chunks = _run_turn(agent, config, ctx, ui, run_id, graph_input)
                async with aclosing(turn_chunks):
                    async for chunk in turn_chunks:
                        yield chunk
    except Exception as exc:
        # The turn couldn't start: the run waits on schema change requests, or e.g. the
        # database or the model's configuration failed.
        if not isinstance(exc, RunWaiting):
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


async def _run_turn(
    agent, config, ctx: ChatContext, ui: ui_stream.UIStream, run_id: int, graph_input
) -> AsyncIterator[ui_stream.Chunk]:
    finished = False
    # Closed however the turn ends, which cancels the graph's running tasks (e.g. a tool call)
    # before the turn is recorded: a stopped turn's response can be dropped while this waits at
    # a `yield`, which doesn't close the stream itself.
    events = agent.astream(
        graph_input,
        config,
        context=ChatDeps(user_id=ctx.created_by, run_id=run_id, data_store_id=ctx.data_store_id),
        stream_mode=["messages", "updates"],
        # Checkpoint each step before the next starts, so the user's message is saved before
        # the model is called.
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
            await _record_turn(agent, config, run_id, finished=finished, partial=ui.partial_reply())


def _run_agent(run_id: int) -> int | None:
    """The run's agent; None if the run is gone."""
    with SessionLocal() as db:
        return db.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))


def _mark_resumed(run_id: int, request_ids: list[uuid.UUID] | None = None) -> None:
    """The run has been given these requests' outcomes (None: all its decided ones'): it takes
    new messages again, unless another still waits."""
    with SessionLocal() as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status.not_in(
                    [SchemaChangeStatus.pending, SchemaChangeStatus.approved]
                ),
                true() if request_ids is None else SchemaChangeRequest.id.in_(request_ids),
            )
            .values(resumed_at=func.now())
        )
        db.commit()


class ResumeFailed(RuntimeError):
    """A resume that couldn't give the run its outcomes (it never got the run to itself): worth
    trying again."""


async def resume_run(run_id: int) -> None:
    """Carry on a run that waits on schema change requests, now some are decided: their tool
    calls return the outcomes, and the turn goes on from there, for the agent's organisation as
    any turn of the run is. The rest of the turn is in the run's history, for its chat to show.

    Raises `ResumeFailed` if the turn couldn't start (the run stays waiting)."""
    agent_id = await run_in_threadpool(_run_agent, run_id)
    if agent_id is None:
        return
    try:
        with SessionLocal() as db:
            ctx = await run_in_threadpool(load_chat_context, db, agent_id, run_id)
    except HTTPException as exc:
        # Its version can't run any more (a tool left the library, say): no turn of it can, so
        # it isn't left waiting on what can't come.
        log.warning("agent run %s can't be resumed: %s", run_id, exc.detail)
        await run_in_threadpool(_mark_resumed, run_id)
        return
    resume = Resume()
    async for chunk in turn_stream(ctx, run_id, resume):
        if chunk["type"] == "error":
            log.warning("resuming agent run %s: %s", run_id, chunk["errorText"])
    if resume.dealt_with is None:
        raise ResumeFailed(f"Agent run {run_id} couldn't be resumed")
    await run_in_threadpool(_mark_resumed, run_id, resume.dealt_with)


@router.post("/agents/{agent_id}/runs/{run_id}/chat")
async def run_chat(
    agent_id: DbId,
    run_id: DbId,
    body: dict,
    user: CurrentUser,
    db: DB,
    org: CurrentOrganisation,
):
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
    ctx = await run_in_threadpool(load_chat_context, db, agent_id, run_id, org=org)
    message = request.messages[-1]
    if message.role != "user":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Expected a user message")
    prompt = ui_stream.human_message(message)
    try:
        await run_in_threadpool(_check_not_waiting, run_id)
    except RunWaiting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, WAITING) from exc
    await run_in_threadpool(_start_turn, run_id, prompt.text)

    return ui_stream.streaming_response(turn_stream(ctx, run_id, prompt))
