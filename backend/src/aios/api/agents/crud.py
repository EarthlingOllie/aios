"""Agents: listing, creating, reading, updating, archiving and deleting them."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aios import checkpoints, dependents
from aios.api.agents.common import (
    AGENT_NAME_INDEX,
    AgentIn,
    AgentOut,
    AgentSummary,
    agent_out,
    get_org_agent,
    lock_agent,
    resolve_skill_versions,
    resolve_tools,
    skill_ids,
    tool_keys,
)
from aios.api.agents.runs import cancel_chat_requests
from aios.api.provenance import archived, made_by, people, provenance
from aios.api.skills import lock_live, unique_name
from aios.api.triggers import delete_triggers, disable_triggers
from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import history
from aios.db import DB, DbId
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Trigger,
    Workflow,
)
from aios.sandbox.sessions import (
    run_sandboxes,
    stop_sandboxes,
)
from aios.versioning import save_agent_version
from aios.workflows import cancel_runs

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
    known = people(db, [i for a, *_ in rows for i in (a.created_by, a.updated_by, a.archived_by)])
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
    with unique_name(db, "an agent", AGENT_NAME_INDEX, clash):
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


@router.get("/agents/{agent_id}/used-by", response_model=dependents.UsedBy)
def get_agent_used_by(agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB):
    """What uses the agent, for the archive confirmation to show: read-only, as archiving is
    never refused."""
    return dependents.used_by(db, get_org_agent(db, org, agent_id), user.id)


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
    lock_agent(db, agent)
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
    # Archived, not deleted: older workflow versions may still pin its versions, so the agent
    # and its versions stay. Its runs are the members' conversations with it, and go now.
    run_ids = list(db.scalars(select(AgentRun.id).where(AgentRun.agent_id == agent.id)))
    sandboxes = run_sandboxes(db, run_ids)
    cancel_chat_requests(db, run_ids)
    history.enqueue_runs(db, AgentRun.agent_id == agent.id, deleting=True)  # their copies go too
    db.execute(delete(AgentRun).where(AgentRun.agent_id == agent.id))  # and their sessions
    delete_triggers(db, Trigger.agent_id == agent.id)
    if agent.archived_at is None:
        agent.archived_at = func.now()
        agent.archived_by = user.id
    db.commit()
    # Turns still underway (a chat's, a schedule's, a cancelled workflow run's node) stop now,
    # rather than carrying on with tool calls for runs that are gone.
    checkpoints.stop_turns(db, run_ids)
    # Their checkpoints aren't tied to the runs, so they don't cascade.
    background.add_task(checkpoints.delete_threads, run_ids)
    # Their session rows went with the runs; the sandboxes would run on until they time out.
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)
