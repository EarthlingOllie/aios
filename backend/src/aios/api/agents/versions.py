"""An agent's versions: listing, reading and restoring them."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from aios.api.agents.common import (
    AgentOut,
    AgentVersionOut,
    agent_out,
    get_agent_version,
    get_org_agent,
    lock_agent,
    skill_count,
    tool_count,
    version_out,
)
from aios.api.agents.runs import build_version_tools
from aios.api.provenance import UserOut, people
from aios.api.skills import lock_live_skills
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB, DbId
from aios.models import (
    AgentRun,
    AgentVersion,
)
from aios.versioning import save_agent_version

router = APIRouter(prefix="/api")


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
    # Also once the agent is archived: workflow versions, and their past runs, still pin its
    # versions.
    agent = get_org_agent(db, org, agent_id)
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
            status.HTTP_409_CONFLICT, "That version uses a skill that has since been archived."
        )
    lock_agent(db, agent)
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
