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
from aios.api.provenance import archived, built_by, people, provenance
from aios.api.skills import unique_name
from aios.api.triggers import disable_triggers
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import Agent, AgentVersion
from aios.db import DB, DbId
from aios.models import AgentRun, Trigger
from aios.versioning import save_agent_version

router = APIRouter(prefix="/api")


def run_counts(db: Session, org_id: int, column, ids) -> dict[int, int]:
    """How many runs (on the platform) each of the organisation's agents or agent versions in
    `ids` has had, by `column` (`AgentRun.agent_id` or `AgentRun.agent_version_id`)."""
    return dict(
        db.execute(
            select(column, func.count(AgentRun.id))
            .where(AgentRun.organisation_id == org_id, column.in_(list(ids)))
            .group_by(column)
        ).all()
    )


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    archived_only: Annotated[bool, Query(alias="archived")] = False,
):
    """The organisation's agents that aren't archived, by name; with `archived`, the archived
    ones instead."""
    rows = store.execute(
        select(
            Agent,
            AgentVersion.version,
            AgentVersion.id,
            skill_ids(AgentVersion.id),
            tool_keys(AgentVersion.id),
        )
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .where(Agent.archived_at.is_not(None) if archived_only else Agent.archived_at.is_(None))
        .order_by(Agent.name)
    ).all()
    runs = run_counts(db, org.id, AgentRun.agent_id, [a.id for a, *_ in rows])
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
            run_count=runs.get(a.id, 0),
            updated_at=a.updated_at,
            **provenance(db, a, known),
            **archived(a, known),
        )
        for a, version, version_id, skills, tools in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(
    body: AgentIn, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    tools = resolve_tools(body.tools)
    skills = resolve_skill_versions(store, body.skill_version_ids)
    refuse_archived_skills(skills)
    agent = Agent(**built_by(user.id), name=body.name, description=body.description)
    clash = name_clash(store, body.name)
    store.add(agent)
    with unique_name(store, "an agent", AGENT_NAME_INDEX, clash):
        save_agent_version(
            store,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            tools=tools,
            note=body.note,
            user_id=user.id,
        )
        store.commit()
    return agent_out(db, org.id, agent)


def name_clash(store: Session, name: str) -> str:
    """What a 409 says if `name` clashes with another of the organisation's agents: an archived
    one's name stays reserved, so it says so."""
    other = store.scalar(select(Agent).where(Agent.name == name))
    archived_note = " (archived)" if other is not None and other.archived_at is not None else ""
    return f"The organisation already has an agent called {name}{archived_note}."


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    return agent_out(db, org.id, get_org_agent(store, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: DbId,
    body: AgentIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Rename/describe the agent; a changed prompt, skill set or tool set creates a new version."""
    agent = get_org_agent(store, agent_id)
    tools = resolve_tools(body.tools, agent.current_version)
    skills = resolve_skill_versions(store, body.skill_version_ids)
    lock_agent(store, agent)
    clash = name_clash(store, body.name)
    agent.name = body.name
    agent.description = body.description
    agent.updated_by = user.id
    before = agent.current_version_id
    with unique_name(store, "an agent", AGENT_NAME_INDEX, clash):
        version = save_agent_version(
            store,
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
        store.commit()
    return agent_out(db, org.id, agent)


@router.post("/agents/{agent_id}/archive", response_model=AgentOut)
def archive_agent(
    agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Archive the agent: nothing new starts on it, and its schedules are paused. Never
    refused, whatever uses it (`GET .../used-by`); runs and turns already going finish, and
    nothing is lost. Archiving an archived agent changes nothing."""
    agent = get_org_agent(store, agent_id)
    # Starting a run share-locks the agent and checks it isn't archived (`lock_live_agent`),
    # so one being started is waited for here, and goes on as already going.
    lock_agent(store, agent)
    if agent.archived_at is None:
        agent.archived_at = func.now()
        agent.archived_by = user.id
        # The store is the source of truth, so it commits first; its schedules (on the
        # platform) are paused after. A schedule saved meanwhile waited for the lock, and is
        # paused here too; one saved after sees the agent archived.
        store.commit()
        disable_triggers(
            db, (Trigger.organisation_id == org.id) & (Trigger.agent_id == agent.id), user.id
        )
        db.commit()
    return agent_out(db, org.id, agent)


@router.post("/agents/{agent_id}/unarchive", response_model=AgentOut)
def unarchive_agent(agent_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    """Bring the agent back. Its schedules stay paused until someone resumes them."""
    agent = get_org_agent(store, agent_id)
    lock_agent(store, agent)
    agent.archived_at = None
    agent.archived_by = None
    store.commit()
    return agent_out(db, org.id, agent)


@router.get("/agents/{agent_id}/used-by", response_model=used_by.AgentUsedBy)
def get_agent_used_by(
    agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """What uses the agent, for the archive confirmation to show: read-only, as archiving is
    never refused."""
    return used_by.agent(db, store, org.id, get_org_agent(store, agent_id), user.id)
