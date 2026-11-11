"""Agents: listing, creating, reading, updating and archiving them. None is ever deleted:
older workflow versions may pin its versions, and its runs are the members' history with it."""

from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios import used_by
from aios.api.agents.common import (
    AGENT_NAME_INDEX,
    AgentIn,
    AgentOut,
    AgentSummary,
    agent_out,
    get_org_agent,
    lock_agent,
    refuse_archived_skills,
    resolve_skill_versions,
    resolve_tools,
    skill_ids,
    tool_keys,
)
from aios.api.provenance import archived, made_by, people, provenance
from aios.api.skills import unique_name
from aios.api.triggers import disable_triggers
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB, DbId
from aios.models import Agent, AgentRun, AgentVersion, Trigger
from aios.versioning import save_agent_version

router = APIRouter(prefix="/api")


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(
    org: CurrentOrganisation,
    db: DB,
    archived_only: Annotated[bool, Query(alias="archived")] = False,
):
    """The organisation's agents that aren't archived, by name; with `archived`, the archived
    ones instead."""
    run_count = (
        select(func.count(AgentRun.id)).where(AgentRun.agent_id == Agent.id).scalar_subquery()
    )
    rows = db.execute(
        select(
            Agent,
            AgentVersion.version,
            AgentVersion.id,
            skill_ids(AgentVersion.id),
            tool_keys(AgentVersion.id),
            run_count,
        )
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .where(
            Agent.organisation_id == org.id,
            Agent.archived_at.is_not(None) if archived_only else Agent.archived_at.is_(None),
        )
        .order_by(Agent.name)
    ).all()
    known = people(
        db, org.id, [i for a, *_ in rows for i in (a.created_by, a.updated_by, a.archived_by)]
    )
    return [
        AgentSummary(
            id=a.id,
            name=a.name,
            description=a.description,
            version=version,
            version_id=version_id,
            skill_count=len(skills or []),
            tool_count=len(tools or []),
            skill_ids=skills or [],
            tool_keys=tools or [],
            run_count=runs,
            updated_at=a.updated_at,
            **provenance(db, a, known),
            **archived(a, known),
        )
        for a, version, version_id, skills, tools, runs in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    tools = resolve_tools(body.tools)
    skills = resolve_skill_versions(db, org, body.skill_version_ids)
    refuse_archived_skills(skills)
    agent = Agent(**made_by(org.id, user.id), name=body.name, description=body.description)
    clash = name_clash(db, org.id, body.name)
    db.add(agent)
    with unique_name(db, "an agent", AGENT_NAME_INDEX, clash):
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


def name_clash(db: Session, org_id: int, name: str) -> str:
    """What a 409 says if `name` clashes with another of the organisation's agents: an archived
    one's name stays reserved, so it says so."""
    other = db.scalar(select(Agent).where(Agent.organisation_id == org_id, Agent.name == name))
    archived_note = " (archived)" if other is not None and other.archived_at is not None else ""
    return f"The organisation already has an agent called {name}{archived_note}."


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
    lock_agent(db, agent)
    clash = name_clash(db, org.id, body.name)
    agent.name = body.name
    agent.description = body.description
    agent.updated_by = user.id
    before = agent.current_version_id
    with unique_name(db, "an agent", AGENT_NAME_INDEX, clash):
        version = save_agent_version(
            db,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            tools=tools,
            note=body.note,
            user_id=user.id,
            model=agent.current_version.model,
        )
        # Keeping an archived skill is fine; making a new version with it isn't.
        if version.id != before:
            refuse_archived_skills(skills)
        db.commit()
    return agent_out(db, agent)


@router.post("/agents/{agent_id}/archive", response_model=AgentOut)
def archive_agent(agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """Archive the agent: nothing new starts on it, and its schedules are paused. Never
    refused, whatever uses it (`GET .../used-by`); runs and turns already going finish, and
    nothing is lost. Archiving an archived agent changes nothing."""
    agent = get_org_agent(db, org, agent_id)
    # Starting a run share-locks the agent and checks it isn't archived (`lock_live_agent`),
    # so one being started is waited for here, and goes on as already going.
    lock_agent(db, agent)
    if agent.archived_at is None:
        agent.archived_at = func.now()
        agent.archived_by = user.id
        disable_triggers(db, Trigger.agent_id == agent.id, user.id)
        db.commit()
    return agent_out(db, agent)


@router.post("/agents/{agent_id}/unarchive", response_model=AgentOut)
def unarchive_agent(agent_id: DbId, org: CurrentOrganisation, db: DB):
    """Bring the agent back. Its schedules stay paused until someone resumes them."""
    agent = get_org_agent(db, org, agent_id)
    lock_agent(db, agent)
    agent.archived_at = None
    agent.archived_by = None
    db.commit()
    return agent_out(db, agent)


@router.get("/agents/{agent_id}/used-by", response_model=used_by.AgentUsedBy)
def get_agent_used_by(agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """What uses the agent, for the archive confirmation to show: read-only, as archiving is
    never refused."""
    return used_by.agent(db, get_org_agent(db, org, agent_id), user.id)
