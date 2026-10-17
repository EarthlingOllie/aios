import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, aliased, selectinload

from aios import checkpoints
from aios.api.skills import lock_live, unique_name
from aios.api.triggers import delete_triggers
from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Trigger,
    Workflow,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunStep,
    WorkflowStepStatus,
    WorkflowVersion,
)
from aios.sandbox.sessions import run_sandboxes, stop_sandboxes
from aios.versioning import NodeSpec, save_workflow_version
from aios.worker import run_workflow_task
from aios.workflows import (
    GraphError,
    RunError,
    alive,
    fail_stopped_runs,
    load_graph,
    prefetch_graph,
    validate_graph,
)

router = APIRouter(prefix="/api")

WORKFLOW_NAME_INDEX = "uq_workflows_owner_id_name_live"


class WorkflowNodeIn(BaseModel):
    key: str
    kind: WorkflowNodeKind
    # The agent version or workflow version the node runs. Pinned, like an agent's skills: a
    # newer version of the agent or subworkflow is taken by saving the workflow with it.
    version_id: DbId


class WorkflowEdgeIn(BaseModel):
    source: str
    target: str


class NodePosition(BaseModel):
    x: FiniteFloat
    y: FiniteFloat


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    nodes: list[WorkflowNodeIn] = []
    edges: list[WorkflowEdgeIn] = []
    # Where the editor draws each node, by key. Not versioned; None keeps the saved layout.
    layout: dict[str, NodePosition] | None = None
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None
    # The version number the edit started from: if the workflow has moved on since, the save is
    # refused rather than overwriting that version. None saves regardless.
    base_version: int | None = None


class WorkflowNodeOut(BaseModel):
    key: str
    kind: WorkflowNodeKind
    version_id: int
    # The agent or workflow the pinned version belongs to.
    target_id: int
    name: str
    version: int
    # The agent's or workflow's current version, or None if it has since been deleted.
    latest_version: int | None
    latest_version_id: int | None


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
    # The workflows whose versions its current version runs, at any depth: another workflow
    # can't run this one if it's among them (`refuse_self_containment`).
    contains: list[int]


class WorkflowOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: WorkflowVersionOut
    # Positions by node key, for the current version's nodes as of the last save that sent
    # them; older versions' nodes may be missing, and the editor lays those out itself.
    layout: dict[str, NodePosition]


def get_owned_workflow(
    db: Session, user_id: int, workflow_id: int, *, deleted_ok: bool = False
) -> Workflow:
    workflow = db.get(Workflow, workflow_id)
    if (
        workflow is None
        or workflow.owner_id != user_id
        or (workflow.deleted_at is not None and not deleted_ok)
    ):
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


def lock_graph(
    db: Session, nodes: Sequence[NodeSpec], workflow: Workflow | None
) -> set[WorkflowNodeKind]:
    """Row-lock what saving `workflow` with these nodes depends on; return the node kinds whose
    agent or workflow has been deleted.

    Locks the agents the nodes run, then the workflows they run together with `workflow` itself
    (unless it's new), so a concurrent delete of any of them waits for this save's commit and
    then sees the usage, or commits first and is seen here. All the workflows are locked in one
    id-ordered pass, so two saves of workflows that run each other can't deadlock; see
    `lock_live` for the lock order. A 404 if `workflow` itself has been deleted meanwhile.
    """
    agent_ids = set(
        db.scalars(
            select(AgentVersion.agent_id).where(
                AgentVersion.id.in_(
                    [n.version_id for n in nodes if n.kind == WorkflowNodeKind.agent]
                )
            )
        )
    )
    workflow_ids = set(
        db.scalars(
            select(WorkflowVersion.workflow_id).where(
                WorkflowVersion.id.in_(
                    [n.version_id for n in nodes if n.kind == WorkflowNodeKind.workflow]
                )
            )
        )
    )
    own = {workflow.id} if workflow is not None and workflow.id is not None else set()
    deleted = set()
    if lock_live(db, Agent, agent_ids) != agent_ids:
        deleted.add(WorkflowNodeKind.agent)
    live = lock_live(db, Workflow, workflow_ids | own)
    if own - live:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if workflow_ids - live:
        deleted.add(WorkflowNodeKind.workflow)
    return deleted


def lock_new_graph(db: Session, nodes: Sequence[NodeSpec], workflow: Workflow | None) -> None:
    """`lock_graph` for a graph from the request: a deleted target is an unknown version."""
    deleted = lock_graph(db, nodes, workflow)
    if WorkflowNodeKind.agent in deleted:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown agent version")
    if WorkflowNodeKind.workflow in deleted:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown workflow version")


def load_version(db: Session, version_id: int) -> WorkflowVersion:
    """A version with everything `version_out` reads, in a fixed number of queries however many
    nodes it has."""
    # Eager loads skip what's already loaded, as rows are after a save, so start from a clean
    # slate. (Not populate_existing: a row reached twice, like a pinned version that is also its
    # agent's current one, would be reset the second time and lazy-load again.)
    db.expire_all()
    nodes = selectinload(WorkflowVersion.nodes)
    return db.scalars(
        select(WorkflowVersion)
        .where(WorkflowVersion.id == version_id)
        .options(
            nodes.selectinload(WorkflowNode.agent_version)
            .selectinload(AgentVersion.agent)
            .selectinload(Agent.current_version),
            nodes.selectinload(WorkflowNode.subworkflow_version)
            .selectinload(WorkflowVersion.workflow)
            .selectinload(Workflow.current_version),
            selectinload(WorkflowVersion.edges),
        )
    ).one()


def node_out(node: WorkflowNode) -> WorkflowNodeOut:
    if node.kind == WorkflowNodeKind.agent:
        pinned: Any = node.agent_version
        target: Any = pinned.agent
    else:
        pinned = node.subworkflow_version
        target = pinned.workflow
    latest = target.current_version if target.deleted_at is None else None
    return WorkflowNodeOut(
        key=node.key,
        kind=node.kind,
        version_id=pinned.id,
        target_id=target.id,
        name=target.name,
        version=pinned.version,
        latest_version=latest.version if latest else None,
        latest_version_id=latest.id if latest else None,
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


def workflow_out(db: Session, workflow: Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        current_version=version_out(load_version(db, workflow.current_version_id)),
        layout=workflow.layout,
    )


def apply_layout(workflow: Workflow, body: WorkflowIn) -> None:
    """Store the request's layout, if it sent one, and keep only the graph's nodes in it."""
    layout = (
        workflow.layout
        if body.layout is None
        else {key: p.model_dump() for key, p in body.layout.items()}
    )
    keys = {n.key for n in body.nodes}
    pruned = {key: p for key, p in layout.items() if key in keys}
    if pruned != workflow.layout:
        workflow.layout = pruned


def node_count(version_id) -> Any:
    return (
        select(func.count())
        .select_from(WorkflowNode)
        .where(WorkflowNode.workflow_version_id == version_id)
        .scalar_subquery()
    )


def workflows_using(db: Session, condition) -> list[str]:
    """Names of the live workflows whose current version has a node matching `condition`.

    Older versions don't count: they keep pinning the versions of a deleted agent or workflow,
    which outlive it (soft delete), and restoring them is refused.
    """
    return list(
        db.scalars(
            select(Workflow.name)
            .join(WorkflowNode, WorkflowNode.workflow_version_id == Workflow.current_version_id)
            .where(Workflow.deleted_at.is_(None), condition)
            .distinct()
            .order_by(Workflow.name)
        )
    )


def contained_workflows(db: Session, user_id: int) -> dict[int, set[int]]:
    """For each of the user's live workflows, the workflows its current version runs, at any
    depth, in one recursive query (and so ending, as in `refuse_self_containment`)."""
    pinned = aliased(WorkflowVersion)
    reach = (
        select(Workflow.id.label("root_id"), pinned.id.label("version_id"), pinned.workflow_id)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == Workflow.current_version_id)
        .join(pinned, pinned.id == WorkflowNode.subworkflow_version_id)
        .where(Workflow.owner_id == user_id, Workflow.deleted_at.is_(None))
        .cte("reach", recursive=True)
    )
    reach = reach.union(
        select(reach.c.root_id, pinned.id, pinned.workflow_id)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .join(pinned, pinned.id == WorkflowNode.subworkflow_version_id)
    )
    contained: dict[int, set[int]] = {}
    for root_id, workflow_id in db.execute(select(reach.c.root_id, reach.c.workflow_id).distinct()):
        contained.setdefault(root_id, set()).add(workflow_id)
    return contained


@router.get("/workflows", response_model=list[WorkflowSummary])
def list_workflows(user: CurrentUser, db: DB):
    rows = db.execute(
        select(
            Workflow, WorkflowVersion.version, WorkflowVersion.id, node_count(WorkflowVersion.id)
        )
        .join(WorkflowVersion, WorkflowVersion.id == Workflow.current_version_id)
        .where(Workflow.owner_id == user.id, Workflow.deleted_at.is_(None))
        .order_by(Workflow.name)
    ).all()
    contained = contained_workflows(db, user.id)
    return [
        WorkflowSummary(
            id=w.id,
            name=w.name,
            description=w.description,
            version=version,
            version_id=version_id,
            node_count=nodes,
            updated_at=w.updated_at,
            contains=sorted(contained.get(w.id, ())),
        )
        for w, version, version_id, nodes in rows
    ]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowIn, user: CurrentUser, db: DB):
    nodes, edges = resolve_graph(db, user.id, body)
    lock_new_graph(db, nodes, None)
    workflow = Workflow(owner_id=user.id, name=body.name, description=body.description, layout={})
    apply_layout(workflow, body)
    db.add(workflow)
    with unique_name(db, "a workflow", WORKFLOW_NAME_INDEX):
        save_workflow_version(
            db, workflow, nodes=nodes, edges=edges, note=body.note, user_id=user.id
        )
        db.commit()
    return workflow_out(db, workflow)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: DbId, user: CurrentUser, db: DB):
    return workflow_out(db, get_owned_workflow(db, user.id, workflow_id))


def refuse_stale_edit(db: Session, workflow: Workflow, base_version: int | None) -> None:
    """Refuse a save of an edit that started from a version that is no longer current."""
    if base_version is None:
        return
    current = db.scalar(
        select(WorkflowVersion.version).where(WorkflowVersion.id == workflow.current_version_id)
    )
    if current != base_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This workflow has changed since you started editing (now v{current}). "
            "Reload to see the changes.",
        )


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: DbId, body: WorkflowIn, user: CurrentUser, db: DB):
    """Rename/describe the workflow; a changed graph creates a new version."""
    workflow = get_owned_workflow(db, user.id, workflow_id)
    # Before checking the graph: an edit of an older version may be invalid only because of what
    # has changed since, and reloading is what fixes it.
    refuse_stale_edit(db, workflow, body.base_version)
    nodes, edges = resolve_graph(db, user.id, body, workflow)
    lock_new_graph(db, nodes, workflow)
    # Another save may have changed the layout, or made a new version, while we waited for the
    # lock; prune that layout, and compare against that version.
    db.refresh(workflow, ["layout", "current_version_id"])
    refuse_stale_edit(db, workflow, body.base_version)
    workflow.name = body.name
    workflow.description = body.description
    apply_layout(workflow, body)
    with unique_name(db, "a workflow", WORKFLOW_NAME_INDEX):
        save_workflow_version(
            db, workflow, nodes=nodes, edges=edges, note=body.note, user_id=user.id
        )
        db.commit()
    return workflow_out(db, workflow)


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: DbId, user: CurrentUser, db: DB) -> None:
    workflow = get_owned_workflow(db, user.id, workflow_id)
    # Hold the row lock across the usage check and the delete; workflow saves take it too.
    if not lock_live(db, Workflow, [workflow.id]):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    used_by = workflows_using(
        db,
        WorkflowNode.subworkflow_version_id.in_(
            select(WorkflowVersion.id).where(WorkflowVersion.workflow_id == workflow.id)
        ),
    )
    if used_by:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Remove this workflow from these workflows first: {', '.join(used_by)}.",
        )
    # Soft delete: older versions of other workflows may still pin this one's versions.
    delete_triggers(db, Trigger.workflow_id == workflow.id)
    workflow.deleted_at = func.now()
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
    # Also once the workflow is deleted: older versions of other workflows, and their past runs,
    # still pin its versions, and a run's subworkflow is shown from the one it pinned.
    workflow = get_owned_workflow(db, user.id, workflow_id, deleted_ok=True)
    return version_out(load_version(db, get_workflow_version(db, workflow, number).id))


@router.post("/workflows/{workflow_id}/versions/{number}/restore", response_model=WorkflowOut)
def restore_workflow_version(workflow_id: DbId, number: DbId, user: CurrentUser, db: DB):
    """Make an old version's graph current again, as a new version (numbers only go up)."""
    workflow = get_owned_workflow(db, user.id, workflow_id)
    old = get_workflow_version(db, workflow, number)
    # The versions its nodes pin still exist (deletes are soft), but the agents and workflows
    # they belong to may not be live any more.
    nodes = [
        NodeSpec(n.key, n.kind, n.agent_version_id or n.subworkflow_version_id) for n in old.nodes
    ]
    edges = [(e.source, e.target) for e in old.edges]
    # The old version passed this check when saved and pins never change, so this only refuses
    # versions saved before the check covered nesting through other workflows.
    refuse_self_containment(
        db, workflow, {n.subworkflow_version_id for n in old.nodes if n.subworkflow_version_id}
    )
    deleted = lock_graph(db, nodes, workflow)
    if deleted:
        what = " and ".join(
            f"{'an' if kind == WorkflowNodeKind.agent else 'a'} {kind}" for kind in sorted(deleted)
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"That version runs {what} that has since been deleted."
        )
    # Keep the positions of the nodes it shares with the current graph, and drop the rest.
    keys = {n.key for n in old.nodes}
    workflow.layout = {key: p for key, p in workflow.layout.items() if key in keys}
    save_workflow_version(
        db,
        workflow,
        nodes=nodes,
        edges=edges,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    db.commit()
    return workflow_out(db, workflow)


# --- Runs ---------------------------------------------------------------------------------------


class WorkflowRunIn(BaseModel):
    # Given to the nodes with no incoming edge, as the user's message to an agent.
    input: str = Field(min_length=1, pattern=r"\S")


class WorkflowRunSummary(BaseModel):
    id: int
    workflow_id: int
    workflow_version_id: int
    workflow_version: int
    status: WorkflowRunStatus
    input: str
    output: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # While it waits, since when; and how long it waited before (seconds), which doesn't count
    # towards its time limit.
    waiting_since: datetime | None
    waited_seconds: float


class WorkflowRunStepOut(BaseModel):
    id: int
    # Node keys from the run's workflow down to this node.
    path: list[str]
    kind: WorkflowNodeKind
    status: WorkflowStepStatus
    input: str
    output: str | None
    error: str | None
    # An agent node's conversation, on the agent's run page; None for workflow nodes, and once
    # the agent run has been deleted (with its agent).
    agent_id: int | None
    agent_run_id: int | None
    # While a waiting agent node waits: the schema change request it waits on.
    waiting_on_request_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None


class StalePin(BaseModel):
    """A version a waiting run pins that's no longer its agent's or workflow's current one: the
    run resumes on it regardless."""

    # The node pinning it, from the run's workflow down.
    path: list[str]
    kind: WorkflowNodeKind
    name: str
    pinned_version: int
    # None once the agent or workflow has been deleted.
    current_version: int | None


class WorkflowRunOut(WorkflowRunSummary):
    # The pinned version's graph.
    version: WorkflowVersionOut
    # In the order they started.
    steps: list[WorkflowRunStepOut]
    # While it waits: the pins, at any depth, it would resume on that are no longer current.
    stale_pins: list[StalePin]


def stale_pins(db: Session, version_id: int) -> list[StalePin]:
    """The versions a run of a workflow version pins that are no longer current: the version
    itself first (path `[]`), then its nodes' at any depth, in node order, each subworkflow's
    after its node. In a fixed number of queries."""
    versions = prefetch_graph(db, version_id)
    agent_ids = {
        n.agent_version.agent_id for v in versions.values() for n in v.nodes if n.agent_version
    }
    workflow_ids = {v.workflow_id for v in versions.values()}

    def current(model, ids: set[int], version_model) -> dict[int, tuple[str, int | None]]:
        rows = db.execute(
            select(model.id, model.name, model.deleted_at, version_model.version)
            .join(version_model, version_model.id == model.current_version_id)
            .where(model.id.in_(ids))
        )
        return {r.id: (r.name, None if r.deleted_at else r.version) for r in rows}

    agents = current(Agent, agent_ids, AgentVersion)
    workflows = current(Workflow, workflow_ids, WorkflowVersion)
    stale: list[StalePin] = []

    def walk(version: WorkflowVersion, path: list[str]) -> None:
        for node in version.nodes:
            if node.kind == WorkflowNodeKind.agent:
                pinned, (name, now) = node.agent_version, agents[node.agent_version.agent_id]
            else:
                pinned = versions[node.subworkflow_version_id]
                name, now = workflows[pinned.workflow_id]
            if now != pinned.version:
                stale.append(
                    StalePin(
                        path=[*path, node.key],
                        kind=node.kind,
                        name=name,
                        pinned_version=pinned.version,
                        current_version=now,
                    )
                )
            if node.kind == WorkflowNodeKind.workflow:
                walk(pinned, [*path, node.key])

    top = versions[version_id]
    name, now = workflows[top.workflow_id]
    if now != top.version:
        stale.append(
            StalePin(
                path=[],
                kind=WorkflowNodeKind.workflow,
                name=name,
                pinned_version=top.version,
                current_version=now,
            )
        )
    walk(top, [])
    return stale


def get_owned_workflow_run(db: Session, user_id: int, workflow_id: int, run_id: int) -> WorkflowRun:
    workflow = get_owned_workflow(db, user_id, workflow_id)
    run = db.get(WorkflowRun, run_id)
    if run is None or run.workflow_id != workflow.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def workflow_run_summary_query():
    return select(
        WorkflowRun.id,
        WorkflowRun.workflow_id,
        WorkflowRun.workflow_version_id,
        WorkflowVersion.version.label("workflow_version"),
        WorkflowRun.status,
        WorkflowRun.input,
        WorkflowRun.output,
        WorkflowRun.error,
        WorkflowRun.created_at,
        WorkflowRun.started_at,
        WorkflowRun.finished_at,
        WorkflowRun.waiting_since,
        WorkflowRun.waited_seconds,
    ).join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)


def workflow_run_summary(db: Session, run_id: int) -> WorkflowRunSummary:
    row = db.execute(workflow_run_summary_query().where(WorkflowRun.id == run_id)).one()
    return WorkflowRunSummary.model_validate(row, from_attributes=True)


@router.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRunSummary])
def list_workflow_runs(workflow_id: DbId, user: CurrentUser, db: DB):
    workflow = get_owned_workflow(db, user.id, workflow_id)
    # Its worker died: shown failed rather than running for ever (see `get_workflow_run`).
    fail_stopped_runs(db, WorkflowRun.workflow_id == workflow.id)
    rows = db.execute(
        workflow_run_summary_query()
        .where(WorkflowRun.workflow_id == workflow.id)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
    ).all()
    return [WorkflowRunSummary.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=WorkflowRunSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_run(workflow_id: DbId, body: WorkflowRunIn, user: CurrentUser, db: DB):
    """Start a run of the workflow's current version. The worker runs it (`aios.workflows`)."""
    workflow = get_owned_workflow(db, user.id, workflow_id)
    run = new_workflow_run(db, workflow, body.input)
    queue_workflow_run(db, run)
    return workflow_run_summary(db, run.id)


def new_workflow_run(db: Session, workflow: Workflow, input: str) -> WorkflowRun:
    """Add (and flush, not commit) a run of the workflow's current version, given `input`.

    Raises `HTTPException` if it can't run: 404 if it has been deleted, 422 if it has no nodes,
    409 if an agent version in its graph can't run. Commit with `queue_workflow_run`; the share
    lock taken here keeps a concurrent delete from leaving a run on a workflow that's gone.
    Also how a schedule starts one (`aios.scheduler`).
    """
    if not lock_live(db, Workflow, [workflow.id], read=True):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    version = workflow.current_version
    if not version.nodes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This workflow has no nodes")
    # Checked now, at every depth, so a version that can't run doesn't leave a run that fails.
    try:
        load_graph(db, version.id)
    except RunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # Pin the version: later edits to the workflow don't change this run.
    run = WorkflowRun(workflow_id=workflow.id, workflow_version_id=version.id, input=input)
    db.add(run)
    db.flush()
    return run


def queue_workflow_run(db: Session, run: WorkflowRun) -> None:
    """Commit the run (with whatever else is pending) and send it to the worker. If it can't be
    queued, delete it and raise a 503."""
    db.commit()
    try:
        run_workflow_task.delay(run.id)
    except Exception as exc:
        # Nothing would ever pick up a committed-but-unqueued run; drop it so the client can
        # simply retry without leaving one stuck in `queued`.
        db.delete(run)
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not queue the run; please try again shortly.",
        ) from exc


@router.get("/workflows/{workflow_id}/runs/{run_id}", response_model=WorkflowRunOut)
def get_workflow_run(workflow_id: DbId, run_id: DbId, user: CurrentUser, db: DB):
    run = get_owned_workflow_run(db, user.id, workflow_id, run_id)
    # A run whose worker died is failed when it's read: its task may never come round again to
    # do it (one requeued as its worker was lost finds the heartbeat still fresh and leaves it).
    # Reads are what show it, and the UI polls a running run, so it's failed within a poll of
    # its heartbeat going stale (`aios.workflows.STALE_AFTER`).
    fail_stopped_runs(db, WorkflowRun.id == run.id)
    summary = workflow_run_summary(db, run.id)
    steps = db.execute(
        select(WorkflowRunStep, AgentRun.agent_id)
        .outerjoin(AgentRun, AgentRun.id == WorkflowRunStep.agent_run_id)
        .where(WorkflowRunStep.run_id == run.id)
        .order_by(WorkflowRunStep.id)
    ).all()
    return WorkflowRunOut(
        **summary.model_dump(),
        version=version_out(load_version(db, summary.workflow_version_id)),
        steps=[
            WorkflowRunStepOut(
                id=step.id,
                path=step.path,
                kind=step.kind,
                status=step.status,
                input=step.input,
                output=step.output,
                error=step.error,
                agent_id=agent_id,
                agent_run_id=step.agent_run_id,
                waiting_on_request_id=step.waiting_on_request_id,
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step, agent_id in steps
        ],
        stale_pins=stale_pins(db, summary.workflow_version_id)
        if summary.status == WorkflowRunStatus.waiting
        else [],
    )


@router.delete("/workflows/{workflow_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow_run(
    workflow_id: DbId, run_id: DbId, user: CurrentUser, db: DB, background: BackgroundTasks
) -> None:
    """Delete the run, and the agent runs its agent nodes held their conversations in."""
    run = get_owned_workflow_run(db, user.id, workflow_id, run_id)
    # Locked before its agent runs are read: a node starting meanwhile records its step and
    # agent run in one transaction, which waits for this lock, so it's either seen here or
    # fails once the run has gone (`aios.workflows._start_step`).
    still_going = db.scalar(select(alive()).where(WorkflowRun.id == run.id).with_for_update())
    # Its agent turns would carry on regardless, and their checkpoints can only be deleted once
    # they end. A queued run can go: the worker won't start a run that's gone (`_claim`); so can
    # one whose worker died, whose heartbeat has stopped.
    if still_going:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This run is still going; wait for it to finish."
        )
    agent_run_ids = list(
        db.scalars(
            select(WorkflowRunStep.agent_run_id).where(
                WorkflowRunStep.run_id == run.id, WorkflowRunStep.agent_run_id.is_not(None)
            )
        )
    )
    sandboxes = run_sandboxes(db, agent_run_ids)
    db.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))  # and their sessions
    db.delete(run)  # and its steps
    db.commit()
    # As for an agent's runs: their checkpoints don't cascade, and their sandboxes would run on.
    background.add_task(checkpoints.delete_threads, agent_run_ids)
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)
