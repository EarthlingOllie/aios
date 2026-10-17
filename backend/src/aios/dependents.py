"""What depends on an agent: what would break, or be cut off, if it were deleted.

`find` lists it, for `GET /api/agents/{id}/dependents` and the guarded delete
(`aios.api.agents.delete_agent`), which refuses until each is resolved:

- its chat runs that are running (a turn underway) or waiting on a schema change request;
- the workflow runs still going (queued, running or waiting) whose pinned version runs one of
  its versions, at any depth;
- the live workflows whose current version runs one of its versions, directly or through a
  nested workflow (whose pinned version does, at any depth), with their schedules;
- its own schedules, which go with it.

Finished runs and older versions don't count: they keep pinning the agent's versions, which
outlive it (it's soft-deleted).
"""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import ColumnElement, false, literal, or_, select
from sqlalchemy.orm import Session

from aios import checkpoints, cron
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    SchemaChangeRequest,
    SchemaChangeStatus,
    Trigger,
    Workflow,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunStep,
    WorkflowVersion,
)
from aios.workflows import alive

# A workflow run that hasn't ended.
GOING = [WorkflowRunStatus.queued, WorkflowRunStatus.running, WorkflowRunStatus.waiting]


class DependentAgentRun(BaseModel):
    """A chat run of the agent that's still going: deleting the agent deletes it."""

    id: int
    title: str | None
    # Waiting on a schema change request, which deleting it cancels.
    waiting: bool
    # A turn is underway (in a chat, or a schedule's): what it does from now on isn't kept.
    running: bool
    trigger_id: int | None
    updated_at: datetime


class DependentWorkflowRun(BaseModel):
    """A workflow run still going that runs the agent: it would fail at the agent's node."""

    id: int
    workflow_id: int
    workflow_name: str
    status: WorkflowRunStatus
    created_at: datetime


class DependentTrigger(BaseModel):
    id: int
    enabled: bool
    cron: str
    description: str
    timezone: str
    next_run_at: datetime | None


class DependentWorkflow(BaseModel):
    """A live workflow whose current version runs the agent: no run of it could start."""

    id: int
    name: str
    version: int
    # Whether one of its own nodes runs the agent, rather than a nested workflow's.
    direct: bool
    # Its schedules, which deactivating it turns off.
    triggers: list[DependentTrigger]


class Dependents(BaseModel):
    agent_runs: list[DependentAgentRun] = []
    workflow_runs: list[DependentWorkflowRun] = []
    workflows: list[DependentWorkflow] = []
    # The agent's own schedules: deleted with it.
    triggers: list[DependentTrigger] = []


def _chat_runs(db: Session, agent: Agent) -> list[DependentAgentRun]:
    """The agent's chat runs (not a workflow node's) that are waiting or have a turn underway."""
    runs = db.execute(
        select(AgentRun.id, AgentRun.title, AgentRun.trigger_id, AgentRun.updated_at)
        .where(
            AgentRun.agent_id == agent.id,
            AgentRun.id.not_in(
                select(WorkflowRunStep.agent_run_id).where(
                    WorkflowRunStep.agent_run_id.is_not(None)
                )
            ),
        )
        .order_by(AgentRun.updated_at.desc(), AgentRun.id.desc())
    ).all()
    ids = [r.id for r in runs]
    # As `aios.api.schema_changes.waiting_on`: not yet given its outcome.
    waiting = set(
        db.scalars(
            select(SchemaChangeRequest.requested_by_run).where(
                SchemaChangeRequest.requested_by_run.in_(ids),
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
            )
        )
    )
    running = checkpoints.turns_underway(db, ids)
    return [
        DependentAgentRun(
            id=r.id,
            title=r.title,
            waiting=r.id in waiting,
            running=r.id in running,
            trigger_id=r.trigger_id,
            updated_at=r.updated_at,
        )
        for r in runs
        if r.id in waiting or r.id in running
    ]


def _running_the_agent(agent: Agent, roots) -> ColumnElement:
    """Which of `roots` (rows of `root_id` and the workflow `version_id` it runs) run one of
    the agent's versions at any depth: a select of their `root_id`s and whether a node of the
    root's own version does (`direct`). One recursive query down the pins, which only point at
    older versions, so the walk ends."""
    reach = roots.cte("reach", recursive=True)
    reach = reach.union(
        select(reach.c.root_id, WorkflowNode.subworkflow_version_id, false())
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .where(WorkflowNode.subworkflow_version_id.is_not(None))
    )
    return (
        select(reach.c.root_id, reach.c.top)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .where(
            WorkflowNode.agent_version_id.in_(
                select(AgentVersion.id).where(AgentVersion.agent_id == agent.id)
            )
        )
        .distinct()
    )


def _workflow_runs(db: Session, agent: Agent) -> list[DependentWorkflowRun]:
    owned = select(Workflow.id).where(Workflow.owner_id == agent.owner_id)
    roots = select(
        WorkflowRun.id.label("root_id"),
        WorkflowRun.workflow_version_id.label("version_id"),
        literal(True).label("top"),
    ).where(
        WorkflowRun.status.in_(GOING),
        # One running whose worker died isn't going any more: it's failed once it's read.
        or_(WorkflowRun.status != WorkflowRunStatus.running, alive()),
        WorkflowRun.workflow_id.in_(owned),
    )
    ids = select(_running_the_agent(agent, roots).subquery().c.root_id)
    rows = db.execute(
        select(WorkflowRun, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(WorkflowRun.id.in_(ids))
        .order_by(WorkflowRun.created_at, WorkflowRun.id)
    ).all()
    return [
        DependentWorkflowRun(
            id=run.id,
            workflow_id=run.workflow_id,
            workflow_name=name,
            status=run.status,
            created_at=run.created_at,
        )
        for run, name in rows
    ]


def _trigger_out(trigger: Trigger) -> DependentTrigger:
    return DependentTrigger(
        id=trigger.id,
        enabled=trigger.enabled,
        cron=trigger.cron,
        description=cron.describe(trigger.cron),
        timezone=trigger.timezone,
        next_run_at=trigger.next_run_at,
    )


def _workflows(db: Session, agent: Agent) -> list[DependentWorkflow]:
    roots = select(
        Workflow.id.label("root_id"),
        Workflow.current_version_id.label("version_id"),
        literal(True).label("top"),
    ).where(Workflow.owner_id == agent.owner_id, Workflow.deleted_at.is_(None))
    using = _running_the_agent(agent, roots).subquery()
    direct: dict[int, bool] = {}
    for workflow_id, top in db.execute(select(using.c.root_id, using.c.top)):
        direct[workflow_id] = direct.get(workflow_id, False) or top
    rows = db.execute(
        select(Workflow, WorkflowVersion.version)
        .join(WorkflowVersion, WorkflowVersion.id == Workflow.current_version_id)
        .where(Workflow.id.in_(direct))
        .order_by(Workflow.name)
    ).all()
    triggers: dict[int, list[DependentTrigger]] = {}
    for trigger in db.scalars(
        select(Trigger).where(Trigger.workflow_id.in_(direct)).order_by(Trigger.id)
    ):
        triggers.setdefault(trigger.workflow_id, []).append(_trigger_out(trigger))
    return [
        DependentWorkflow(
            id=workflow.id,
            name=workflow.name,
            version=version,
            direct=direct[workflow.id],
            triggers=triggers.get(workflow.id, []),
        )
        for workflow, version in rows
    ]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def unresolved(
    found: Dependents,
    *,
    cancel_agent_runs: set[int],
    cancel_workflow_runs: set[int],
    deactivate_workflows: set[int],
) -> str | None:
    """Why the agent can't be deleted with these resolutions, or None if it can: what's still
    going that isn't to be cancelled, and the workflows that run it and aren't to be
    deactivated (they must be edited first)."""
    chats = [r for r in found.agent_runs if r.id not in cancel_agent_runs]
    runs = [r for r in found.workflow_runs if r.id not in cancel_workflow_runs]
    flows = [w.name for w in found.workflows if w.id not in deactivate_workflows]
    parts = []
    if chats:
        parts.append(f"{_count(len(chats), 'chat run')} still going (cancel them)")
    if runs:
        parts.append(f"{_count(len(runs), 'workflow run')} still going (cancel them)")
    if flows:
        parts.append(
            f"{'workflows' if len(flows) > 1 else 'the workflow'} {', '.join(flows)}"
            " (deactivate or edit them to stop running it)"
        )
    if not parts:
        return None
    return f"This agent is still in use: {'; '.join(parts)}."


def find(db: Session, agent: Agent) -> Dependents:
    own = db.scalars(select(Trigger).where(Trigger.agent_id == agent.id).order_by(Trigger.id))
    return Dependents(
        agent_runs=_chat_runs(db, agent),
        workflow_runs=_workflow_runs(db, agent),
        workflows=_workflows(db, agent),
        triggers=[_trigger_out(t) for t in own],
    )
