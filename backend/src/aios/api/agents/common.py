"""What the agents API's parts share: the agent's schemas, lookups and locks."""

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg
from sqlalchemy.orm import Session, object_session

from aios.api.provenance import Archived, Provenance, UserOut, archived, people, provenance
from aios.api.skills import lock_live_skills
from aios.db import DbId
from aios.models import (
    Agent,
    AgentVersion,
    AgentVersionTool,
    Organisation,
    Skill,
    SkillVersion,
    agent_version_skills,
)
from aios.tools import (
    TOOLS,
    ToolError,
    check_stored_tool,
    validate_tool,
)

AGENT_NAME_INDEX = "uq_agents_organisation_id_name"


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
    # The skill's current version, or None if the skill has since been archived.
    latest_version: int | None
    latest_version_id: int | None


class AgentToolOut(BaseModel):
    key: str
    name: str
    description: str
    config: dict[str, Any]
    # False if the tool has since been removed from the library.
    available: bool


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


class AgentSummary(Provenance, Archived):
    id: int
    name: str
    description: str
    version: int
    # The current version's id, which a workflow node pins to run this agent.
    version_id: int
    skill_count: int
    tool_count: int
    # What the current version has, in its order: its skills (any version of each) and tools.
    skill_ids: list[int]
    tool_keys: list[str]
    run_count: int
    updated_at: datetime


class AgentOut(Provenance, Archived):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: AgentVersionOut


def get_org_agent(db: Session, org: Organisation, agent_id: int) -> Agent:
    """The organisation's agent, archived or not; a 404 otherwise, as if it didn't exist."""
    agent = db.get(Agent, agent_id)
    if agent is None or agent.organisation_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return agent


def archived_refusal(kind: str, name: str) -> str:
    """Why nothing new can start on an archived agent or workflow."""
    return f"The {kind} {name} is archived."


def refuse_if_archived(agent: Agent) -> None:
    """A 409 if the agent is archived: it starts no new turns."""
    if agent.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, archived_refusal("agent", agent.name))


def lock_agent(db: Session, agent: Agent, *, read: bool = False) -> None:
    """Row-lock the agent until commit (see `lock_live`), refreshing it, archived or not."""
    db.execute(
        select(Agent.id)
        .where(Agent.id == agent.id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )
    db.refresh(agent)


def lock_live_agent(db: Session, agent: Agent, *, read: bool = False) -> None:
    """Row-lock the agent until commit (see `lock_live`); a 409 if it's archived, for something
    new to start on it. Archiving takes the lock too, so it waits for a start in flight, which
    then counts as already going."""
    lock_agent(db, agent, read=read)
    refuse_if_archived(agent)


def get_agent_version(db: Session, agent: Agent, number: int) -> AgentVersion:
    version = db.scalar(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id, AgentVersion.version == number
        )
    )
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return version


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
                Skill.archived_at.is_(None),
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
    # Lock the skills until commit and re-check they aren't archived, so a concurrent archive
    # either waits for this save or wins and we reject the attach.
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
        latest = skill.current_version if skill.archived_at is None else None
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
    known = people(db, [agent.created_by, agent.updated_by, agent.archived_by, version.created_by])
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        current_version=version_out(version, known),
        **provenance(db, agent, known),
        **archived(agent, known),
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


def skill_ids(version_id) -> Any:
    """The ids of the skills a version has, in order (null if it has none)."""
    return (
        select(
            array_agg(aggregate_order_by(SkillVersion.skill_id, agent_version_skills.c.position))
        )
        .select_from(agent_version_skills)
        .join(SkillVersion, SkillVersion.id == agent_version_skills.c.skill_version_id)
        .where(agent_version_skills.c.agent_version_id == version_id)
        .scalar_subquery()
    )


def tool_keys(version_id) -> Any:
    """The keys of the tools a version has, in order (null if it has none)."""
    return (
        select(array_agg(aggregate_order_by(AgentVersionTool.tool_key, AgentVersionTool.position)))
        .where(AgentVersionTool.agent_version_id == version_id)
        .scalar_subquery()
    )
