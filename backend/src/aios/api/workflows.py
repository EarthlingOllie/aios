from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from aios.api.skills import unique_name
from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Agent,
    AgentVersion,
    Workflow,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowVersion,
)
from aios.versioning import NodeSpec, save_workflow_version
from aios.workflows import GraphError, validate_graph

router = APIRouter(prefix="/api")


class WorkflowNodeIn(BaseModel):
    key: str
    kind: WorkflowNodeKind
    # The agent version or workflow version the node runs. Pinned, like an agent's skills: a
    # newer version of the agent or subworkflow is taken by saving the workflow with it.
    version_id: DbId


class WorkflowEdgeIn(BaseModel):
    source: str
    target: str


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    nodes: list[WorkflowNodeIn] = []
    edges: list[WorkflowEdgeIn] = []
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class WorkflowNodeOut(BaseModel):
    key: str
    kind: WorkflowNodeKind
    version_id: int
    # The agent or workflow the pinned version belongs to.
    target_id: int
    name: str
    version: int
    latest_version: int
    latest_version_id: int


class WorkflowEdgeOut(BaseModel):
    source: str
    target: str


class WorkflowVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    node_count: int


class WorkflowVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    nodes: list[WorkflowNodeOut]
    edges: list[WorkflowEdgeOut]


class WorkflowSummary(BaseModel):
    id: int
    name: str
    description: str
    version: int
    version_id: int
    node_count: int
    updated_at: datetime


class WorkflowOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: WorkflowVersionOut


def get_owned_workflow(db: Session, user_id: int, workflow_id: int) -> Workflow:
    workflow = db.get(Workflow, workflow_id)
    if workflow is None or workflow.owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return workflow


def get_workflow_version(db: Session, workflow: Workflow, number: int) -> WorkflowVersion:
    version = db.scalar(
        select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.version == number
        )
    )
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return version


def refuse_self_containment(db: Session, workflow: Workflow, version_ids: set[int]) -> None:
    """Refuse pinning these workflow versions in `workflow` if any of them is, or runs at any
    depth, a version of `workflow` itself.

    One recursive query walks down the pins, remembering which directly pinned workflow each
    reached version came through, so the error can name it. Pins only point at versions that
    existed when they were saved, so the walk always ends; UNION drops repeats, so a version
    reachable along many paths is expanded once per directly pinned workflow."""
    if not version_ids:
        return
    pinned = aliased(WorkflowVersion)
    reach = (
        select(
            WorkflowVersion.id.label("version_id"),
            WorkflowVersion.workflow_id.label("workflow_id"),
            WorkflowVersion.workflow_id.label("via_id"),
        )
        .where(WorkflowVersion.id.in_(version_ids))
        .cte("reach", recursive=True)
    )
    reach = reach.union(
        select(pinned.id, pinned.workflow_id, reach.c.via_id)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .join(pinned, pinned.id == WorkflowNode.subworkflow_version_id)
        .where(reach.c.workflow_id != workflow.id)
    )
    via = db.execute(
        select(Workflow.id, Workflow.name)
        .join(reach, reach.c.via_id == Workflow.id)
        .where(reach.c.workflow_id == workflow.id)
        .distinct()
        .order_by(Workflow.name)
    ).all()
    if not via:
        return
    if any(workflow_id == workflow.id for workflow_id, _ in via):
        detail = "A workflow can't contain itself"
    else:
        names = ", ".join(name for _, name in via)
        detail = f"A workflow can't contain itself, even through another workflow (via {names})"
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def resolve_graph(
    db: Session, user_id: int, body: WorkflowIn, workflow: Workflow | None = None
) -> tuple[list[NodeSpec], list[tuple[str, str]]]:
    """The requested graph, checked: its shape, and that each node pins a version of one of the
    user's agents or of another of their workflows."""
    edges = [(e.source, e.target) for e in body.edges]
    try:
        validate_graph([n.key for n in body.nodes], edges)
    except GraphError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    agent_ids = {n.version_id for n in body.nodes if n.kind == WorkflowNodeKind.agent}
    workflow_ids = {n.version_id for n in body.nodes if n.kind == WorkflowNodeKind.workflow}
    found_agents = set(
        db.scalars(
            select(AgentVersion.id)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(AgentVersion.id.in_(agent_ids), Agent.owner_id == user_id)
        )
    )
    if found_agents != agent_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown agent version")
    found_workflows = {
        version_id: workflow_id
        for version_id, workflow_id in db.execute(
            select(WorkflowVersion.id, WorkflowVersion.workflow_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(WorkflowVersion.id.in_(workflow_ids), Workflow.owner_id == user_id)
        )
    }
    if set(found_workflows) != workflow_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown workflow version")
    if workflow is not None:
        refuse_self_containment(db, workflow, workflow_ids)
    return [NodeSpec(n.key, n.kind, n.version_id) for n in body.nodes], edges


def node_out(node: WorkflowNode) -> WorkflowNodeOut:
    if node.kind == WorkflowNodeKind.agent:
        pinned: Any = node.agent_version
        target: Any = pinned.agent
    else:
        pinned = node.subworkflow_version
        target = pinned.workflow
    return WorkflowNodeOut(
        key=node.key,
        kind=node.kind,
        version_id=pinned.id,
        target_id=target.id,
        name=target.name,
        version=pinned.version,
        latest_version=target.current_version.version,
        latest_version_id=target.current_version.id,
    )


def version_out(version: WorkflowVersion) -> WorkflowVersionOut:
    return WorkflowVersionOut(
        id=version.id,
        version=version.version,
        note=version.note,
        created_at=version.created_at,
        nodes=[node_out(n) for n in version.nodes],
        edges=[WorkflowEdgeOut(source=e.source, target=e.target) for e in version.edges],
    )


def workflow_out(workflow: Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        current_version=version_out(workflow.current_version),
    )


def node_count(version_id) -> Any:
    return (
        select(func.count())
        .select_from(WorkflowNode)
        .where(WorkflowNode.workflow_version_id == version_id)
        .scalar_subquery()
    )


def workflows_using(db: Session, condition) -> list[str]:
    """Names of the workflows with a version that has a node matching `condition`."""
    return list(
        db.scalars(
            select(Workflow.name)
            .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
            .join(WorkflowNode, WorkflowNode.workflow_version_id == WorkflowVersion.id)
            .where(condition)
            .distinct()
            .order_by(Workflow.name)
        )
    )


@router.get("/workflows", response_model=list[WorkflowSummary])
def list_workflows(user: CurrentUser, db: DB):
    rows = db.execute(
        select(
            Workflow, WorkflowVersion.version, WorkflowVersion.id, node_count(WorkflowVersion.id)
        )
        .join(WorkflowVersion, WorkflowVersion.id == Workflow.current_version_id)
        .where(Workflow.owner_id == user.id)
        .order_by(Workflow.name)
    ).all()
    return [
        WorkflowSummary(
            id=w.id,
            name=w.name,
            description=w.description,
            version=version,
            version_id=version_id,
            node_count=nodes,
            updated_at=w.updated_at,
        )
        for w, version, version_id, nodes in rows
    ]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowIn, user: CurrentUser, db: DB):
    nodes, edges = resolve_graph(db, user.id, body)
    workflow = Workflow(owner_id=user.id, name=body.name, description=body.description)
    db.add(workflow)
    with unique_name(db, "a workflow"):
        save_workflow_version(
            db, workflow, nodes=nodes, edges=edges, note=body.note, user_id=user.id
        )
        db.commit()
    return workflow_out(workflow)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: DbId, user: CurrentUser, db: DB):
    return workflow_out(get_owned_workflow(db, user.id, workflow_id))


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: DbId, body: WorkflowIn, user: CurrentUser, db: DB):
    """Rename/describe the workflow; a changed graph creates a new version."""
    workflow = get_owned_workflow(db, user.id, workflow_id)
    nodes, edges = resolve_graph(db, user.id, body, workflow)
    workflow.name = body.name
    workflow.description = body.description
    with unique_name(db, "a workflow"):
        save_workflow_version(
            db, workflow, nodes=nodes, edges=edges, note=body.note, user_id=user.id
        )
        db.commit()
    return workflow_out(workflow)


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: DbId, user: CurrentUser, db: DB) -> None:
    workflow = get_owned_workflow(db, user.id, workflow_id)
    used_by = workflows_using(
        db,
        WorkflowNode.subworkflow_version_id.in_(
            select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == workflow.id)
        ),
    )
    if used_by:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"These workflows run this one as a step: {', '.join(used_by)}.",
        )
    workflow.current_version_id = None  # break the workflows <-> workflow_versions cycle first
    db.flush()
    db.delete(workflow)
    db.commit()


@router.get("/workflows/{workflow_id}/versions", response_model=list[WorkflowVersionSummary])
def list_workflow_versions(workflow_id: DbId, user: CurrentUser, db: DB):
    workflow = get_owned_workflow(db, user.id, workflow_id)
    rows = db.execute(
        select(WorkflowVersion, node_count(WorkflowVersion.id))
        .where(WorkflowVersion.workflow_id == workflow.id)
        .order_by(WorkflowVersion.version.desc())
    ).all()
    return [
        WorkflowVersionSummary(
            id=v.id, version=v.version, note=v.note, created_at=v.created_at, node_count=nodes
        )
        for v, nodes in rows
    ]


@router.get("/workflows/{workflow_id}/versions/{number}", response_model=WorkflowVersionOut)
def get_workflow_version_detail(workflow_id: DbId, number: DbId, user: CurrentUser, db: DB):
    workflow = get_owned_workflow(db, user.id, workflow_id)
    return version_out(get_workflow_version(db, workflow, number))


@router.post("/workflows/{workflow_id}/versions/{number}/restore", response_model=WorkflowOut)
def restore_workflow_version(workflow_id: DbId, number: DbId, user: CurrentUser, db: DB):
    """Make an old version's graph current again, as a new version (numbers only go up)."""
    workflow = get_owned_workflow(db, user.id, workflow_id)
    old = get_workflow_version(db, workflow, number)
    # The versions its nodes pin still exist: deleting an agent or workflow in use is refused.
    nodes = [
        NodeSpec(n.key, n.kind, n.agent_version_id or n.subworkflow_version_id) for n in old.nodes
    ]
    edges = [(e.source, e.target) for e in old.edges]
    # The old version passed this check when saved and pins never change, so this only refuses
    # versions saved before the check covered nesting through other workflows.
    refuse_self_containment(
        db, workflow, {n.subworkflow_version_id for n in old.nodes if n.subworkflow_version_id}
    )
    save_workflow_version(
        db,
        workflow,
        nodes=nodes,
        edges=edges,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    db.commit()
    return workflow_out(workflow)
