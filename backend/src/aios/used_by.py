"""What uses an agent, workflow or skill: the read-only list its archive confirmation shows.

Archiving is never refused over any of it (`POST /api/{agents,workflows,skills}/{id}/archive`);
the list only says what will stop working.

- An agent (`GET /api/agents/{id}/used-by`): the live workflows whose current version runs one
  of its versions, at any depth, with their schedules; its own schedules; the chats it's a
  member of; and the data tables it will leave read-only (none until tables have owners).
- A workflow (`GET /api/workflows/{id}/used-by`): the live workflows whose current version nests
  one of its versions, at any depth, with their schedules; its own schedules; and the chats it's
  a member of.
- A skill (`GET /api/skills/{id}/used-by`): the agents (not archived) whose current version uses
  some version of it (`aios.api.skills.skill_usage`).

Workflows are looked for in the item's organisation's store, whichever member made them: a
workflow can only run its own organisation's agents and workflows. Schedules and chats are on the
platform, and name the item by its id in that store, so they're looked for in its organisation.
Older versions don't count: they keep pinning the item's versions, which outlive it (it's
archived, not deleted). Archived workflows don't count either: they start nothing. Of the chats,
the member asking sees the public ones and the private ones they're in.
"""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, false, literal, or_, select
from sqlalchemy.orm import Session

from aios import cron
from aios.datastores.models import Agent, AgentVersion, Workflow, WorkflowNode, WorkflowVersion
from aios.models import Chat, ChatMember, ChatVisibility, Trigger


class UsedByTrigger(BaseModel):
    id: int
    enabled: bool
    cron: str
    description: str
    timezone: str
    next_run_at: datetime | None


class UsedByWorkflow(BaseModel):
    """A live workflow whose current version runs the item: no run of it could start once the
    item is archived."""

    id: int
    name: str
    version: int
    # Whether one of its own nodes runs the item, rather than a nested workflow's.
    direct: bool
    # Its schedules, which archiving the item leaves as they are (their firings will fail).
    triggers: list[UsedByTrigger]


class UsedByChat(BaseModel):
    id: int
    name: str


class UsedByTable(BaseModel):
    """A data table the agent owns, which archiving it leaves read-only."""

    name: str


class AgentUsedBy(BaseModel):
    """What uses an agent, as its archive confirmation shows it."""

    # The live workflows that run it at any depth, with their schedules.
    workflows: list[UsedByWorkflow]
    # Its own schedules, which archiving it pauses.
    triggers: list[UsedByTrigger]
    # The chats it's a member of.
    chats: list[UsedByChat]
    # The data tables it owns: none until tables have owners.
    tables: list[UsedByTable]


class WorkflowUsedBy(BaseModel):
    """What uses a workflow, as its archive confirmation shows it."""

    # The live workflows that nest it at any depth, with their schedules.
    workflows: list[UsedByWorkflow]
    # Its own schedules, which archiving it pauses.
    triggers: list[UsedByTrigger]
    # The chats it's a member of.
    chats: list[UsedByChat]


def _trigger_out(trigger: Trigger) -> UsedByTrigger:
    return UsedByTrigger(
        id=trigger.id,
        enabled=trigger.enabled,
        cron=trigger.cron,
        description=cron.describe(trigger.cron),
        timezone=trigger.timezone,
        next_run_at=trigger.next_run_at,
    )


def _workflows_running(pins: ColumnElement) -> Select:
    """The live workflows (in the organisation's store) whose current version has a node
    matching `pins`, at any depth, by name. One recursive query down the pins, which only point
    at older versions, so the walk ends; `top` marks the root's own version's nodes."""
    roots = select(
        Workflow.id.label("root_id"),
        Workflow.current_version_id.label("version_id"),
        literal(True).label("top"),
    ).where(Workflow.archived_at.is_(None))
    reach = roots.cte("reach", recursive=True)
    reach = reach.union(
        select(reach.c.root_id, WorkflowNode.subworkflow_version_id, false())
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .where(WorkflowNode.subworkflow_version_id.is_not(None))
    )
    return (
        select(reach.c.root_id, reach.c.top)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .where(pins)
    )


def _workflows(
    db: Session, store: Session, org_id: int, pins: ColumnElement
) -> list[UsedByWorkflow]:
    direct: dict[int, bool] = {}
    for workflow_id, top in store.execute(_workflows_running(pins).distinct()):
        direct[workflow_id] = direct.get(workflow_id, False) or top
    rows = store.execute(
        select(Workflow, WorkflowVersion.version)
        .join(WorkflowVersion, WorkflowVersion.id == Workflow.current_version_id)
        .where(Workflow.id.in_(direct))
        .order_by(Workflow.name)
    ).all()
    triggers: dict[int, list[UsedByTrigger]] = {}
    for trigger in db.scalars(
        select(Trigger)
        .where(Trigger.organisation_id == org_id, Trigger.workflow_id.in_(direct))
        .order_by(Trigger.id)
    ):
        triggers.setdefault(trigger.workflow_id, []).append(_trigger_out(trigger))
    return [
        UsedByWorkflow(
            id=workflow.id,
            name=workflow.name,
            version=version,
            direct=direct[workflow.id],
            triggers=triggers.get(workflow.id, []),
        )
        for workflow, version in rows
    ]


def _triggers(db: Session, *where: ColumnElement[bool]) -> list[UsedByTrigger]:
    return [_trigger_out(t) for t in db.scalars(select(Trigger).where(*where).order_by(Trigger.id))]


def _chats(db: Session, org_id: int, member: ColumnElement, user_id: int) -> list[UsedByChat]:
    """The organisation's chats with a member matching `member`, as the member `user_id` may see
    them."""
    theirs = select(ChatMember.chat_id).where(ChatMember.user_id == user_id)
    rows = db.execute(
        select(Chat.id, Chat.name)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(
            Chat.organisation_id == org_id,
            member,
            or_(Chat.visibility == ChatVisibility.public, Chat.id.in_(theirs)),
        )
        .order_by(Chat.name, Chat.id)
    ).all()
    return [UsedByChat(id=c.id, name=c.name) for c in rows]


def agent(db: Session, store: Session, org_id: int, agent: Agent, user_id: int) -> AgentUsedBy:
    """What uses the agent (in the organisation `org_id`'s store, `store`), as the member
    `user_id` may see it: its workflows from the store, its schedules and chats from the
    platform (`db`)."""
    versions = select(AgentVersion.id).where(AgentVersion.agent_id == agent.id)
    return AgentUsedBy(
        workflows=_workflows(db, store, org_id, WorkflowNode.agent_version_id.in_(versions)),
        triggers=_triggers(db, Trigger.organisation_id == org_id, Trigger.agent_id == agent.id),
        chats=_chats(db, org_id, ChatMember.agent_id == agent.id, user_id),
        tables=[],
    )


def workflow(
    db: Session, store: Session, org_id: int, workflow: Workflow, user_id: int
) -> WorkflowUsedBy:
    """What uses the workflow (in the organisation `org_id`'s store, `store`), as the member
    `user_id` may see it."""
    versions = select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == workflow.id)
    return WorkflowUsedBy(
        workflows=_workflows(db, store, org_id, WorkflowNode.subworkflow_version_id.in_(versions)),
        triggers=_triggers(
            db, Trigger.organisation_id == org_id, Trigger.workflow_id == workflow.id
        ),
        chats=_chats(db, org_id, ChatMember.workflow_id == workflow.id, user_id),
    )
