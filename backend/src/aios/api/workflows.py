import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field, FiniteFloat, model_serializer, model_validator
from sqlalchemy import Row, delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased, object_session, selectinload

from aios import approvals, audit, checkpoints, invocations, limits, run_service, used_by
from aios.api.agents.common import apply_guards, organisation_limits
from aios.api.agents.runs import InvocationRecordOut, run_chats
from aios.api.provenance import (
    Archived,
    Provenance,
    UserOut,
    archived,
    built_by,
    people,
    provenance,
    started_by,
)
from aios.api.skills import lock_live, unique_name
from aios.api.triggers import disable_triggers, unindex
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import schema_changes, store_id_of
from aios.datastores.models import (
    Agent,
    AgentRun,
    AgentVersion,
    AgentVersionTool,
    Event,
    Invocation,
    SchemaChangeRequest,
    Trigger,
    Workflow,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from aios.db import DB, DbId, SessionLocal
from aios.limits import RunLimits
from aios.models import (
    InvocableBy,
    InvocationStatus,
    Role,
    SchemaChangeStatus,
    TriggerTargetKind,
    WorkflowNodeKind,
    WorkflowRunStatus,
    WorkflowStepStatus,
)
from aios.permissions import Action, CurrentRole, allow
from aios.sandbox.sessions import run_sandboxes, stop_sandboxes
from aios.tools import ToolOrganisation, unusable_tools
from aios.versioning import NodeSpec, save_workflow_version
from aios.workflows import (
    NOT_ENDED,
    GraphError,
    RunError,
    _end_runs,
    _record_ended,
    advance_work,
    alive,
    archived_in_graph,
    close_requests,
    load_graph,
    reachable_versions,
    validate_graph,
)
from aios.workflows.structured import (
    NodeDeclared,
    check_workflow,
    compact,
    describe,
    schema_errors,
    template_errors,
)

router = APIRouter(prefix="/api")

# How long an approval node's prompt template may be.
PROMPT_MAX_LENGTH = 10_000
MAX_EXPIRY_DAYS = approvals.MAX_EXPIRY_DAYS

WORKFLOW_NAME_INDEX = "uq_workflows_name"


class WorkflowNodeIn(BaseModel):
    key: str
    kind: WorkflowNodeKind
    # The agent version or workflow version the node runs. Pinned, like an agent's skills: a
    # newer version of the agent or subworkflow is taken by saving the workflow with it. None
    # for an approval node, which pins nothing.
    version_id: DbId | None = None
    # What it takes and gives (JSON Schemas), and the template its input is built from, if it
    # declares them (`aios.workflows.structured`). An agent node with an output schema gives
    # structured output.
    input_schema: Any = None
    output_schema: Any = None
    input_template: Any = None
    # An approval node's (`aios.approvals`): the prompt its approvers see (a template, with
    # placeholders as an input template's), the least role that may decide it (editor, the
    # default, or admin), and how many days it waits before it expires (None: the
    # organisation's setting). Only an approval node has them.
    prompt_template: str | None = Field(default=None, max_length=PROMPT_MAX_LENGTH)
    approver_role: Literal["editor", "admin"] | None = None
    expiry_days: int | None = Field(default=None, ge=1, le=MAX_EXPIRY_DAYS)


class WorkflowEdgeIn(BaseModel):
    source: str
    target: str
    # A JSONLogic condition on the source's output and the run's input; None: always active.
    condition: Any = None


class NodePosition(BaseModel):
    x: FiniteFloat
    y: FiniteFloat


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    nodes: list[WorkflowNodeIn] = []
    edges: list[WorkflowEdgeIn] = []
    # JSON Schemas for what its runs take and give, if it declares them.
    input_schema: Any = None
    output_schema: Any = None
    # Where the editor draws each node, by key. Not versioned; None keeps the saved layout.
    layout: dict[str, NodePosition] | None = None
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None
    # The version number the edit started from: if the workflow has moved on since, the save is
    # refused rather than overwriting that version. None saves regardless.
    base_version: int | None = None
    # Who may invoke it by mentioning it (`aios.guards`); None keeps it as it is (everyone, for
    # a new workflow).
    invocable_by: InvocableBy | None = None
    # Its own run caps (`aios.limits`), each null to follow the organisation's; None keeps them
    # as they are. Only admins may set one above the organisation's (403).
    limits: RunLimits | None = None


class WorkflowNodeOut(BaseModel):
    key: str
    kind: WorkflowNodeKind
    # None for an approval node, which pins nothing.
    version_id: int | None
    # The agent or workflow the pinned version belongs to.
    target_id: int | None
    name: str
    version: int | None
    # The agent's or workflow's current version (None only while one is being made).
    latest_version: int | None
    latest_version_id: int | None
    input_schema: Any = None
    output_schema: Any = None
    input_template: Any = None
    # An approval node's.
    prompt_template: str | None = None
    approver_role: str | None = None
    expiry_days: int | None = None


class WorkflowEdgeOut(BaseModel):
    source: str
    target: str
    # Only when it has one.
    condition: Any = None

    @model_serializer(mode="wrap")
    def _without_no_condition(self, handler) -> dict[str, Any]:
        out = handler(self)
        if out.get("condition") is None:
            out.pop("condition", None)
        return out


class WorkflowVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    # The member who saved it.
    created_by: UserOut | None
    node_count: int


class WorkflowVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    # The member who saved it.
    created_by: UserOut | None
    nodes: list[WorkflowNodeOut]
    edges: list[WorkflowEdgeOut]
    input_schema: Any = None
    output_schema: Any = None
    # Why a run of the version can't start: an agent it runs, at any depth, can't run in the
    # organisation (a tool it needs is missing, as an imported version's may be); None if not.
    unrunnable: str | None = None


class WorkflowSummary(Provenance, Archived):
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


class WorkflowOut(Provenance, Archived):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: WorkflowVersionOut
    # Positions by node key, for the current version's nodes as of the last save that sent
    # them; older versions' nodes may be missing, and the editor lays those out itself.
    layout: dict[str, NodePosition]
    invocable_by: InvocableBy
    # Its own run caps (null follows the organisation's), and the caps in force for its runs.
    limits: RunLimits
    limits_in_force: RunLimits


def get_org_workflow(store: Session, workflow_id: int) -> Workflow:
    """The workflow in the organisation's store (`store`), archived or not; a 404 otherwise. Ids
    are the store's own, so another organisation's workflow is never reached, whatever its id."""
    workflow = store.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return workflow


def archived_refusal(workflow: Workflow) -> str:
    """Why nothing new can start on an archived workflow."""
    return f"The workflow {workflow.name} is archived."


def lock_workflow(db: Session, workflow: Workflow, *, read: bool = False) -> None:
    """Row-lock the workflow until commit (see `lock_live`), refreshing it, archived or not."""
    db.execute(
        select(Workflow.id)
        .where(Workflow.id == workflow.id)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )
    db.refresh(workflow)


def lock_live_workflow(db: Session, workflow: Workflow) -> None:
    """Share-lock the workflow until commit; a 409 if it's archived, for a run to start on it.
    Archiving takes the lock too, so it waits for a start in flight, which then counts as
    already going."""
    lock_workflow(db, workflow, read=True)
    if workflow.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, archived_refusal(workflow))


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
    db: Session, body: WorkflowIn, workflow: Workflow | None = None
) -> tuple[list[NodeSpec], list[tuple[str, str]]]:
    """The requested graph, checked: its shape, and that each node pins a version of one of the
    organisation's agents or of another of its workflows (in its store, `db`)."""
    edges = [(e.source, e.target) for e in body.edges]
    try:
        validate_graph([n.key for n in body.nodes], edges)
    except GraphError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    for n in body.nodes:
        if n.kind != WorkflowNodeKind.approval and n.version_id is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Node {n.key!r} needs the {n.kind} version it runs",
            )
    agent_ids = {n.version_id for n in body.nodes if n.kind == WorkflowNodeKind.agent}
    workflow_ids = {n.version_id for n in body.nodes if n.kind == WorkflowNodeKind.workflow}
    found_agents = set(db.scalars(select(AgentVersion.id).where(AgentVersion.id.in_(agent_ids))))
    if found_agents != agent_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown agent version")
    found_workflows = {
        version_id: workflow_id
        for version_id, workflow_id in db.execute(
            select(WorkflowVersion.id, WorkflowVersion.workflow_id).where(
                WorkflowVersion.id.in_(workflow_ids)
            )
        )
    }
    if set(found_workflows) != workflow_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown workflow version")
    if workflow is not None:
        refuse_self_containment(db, workflow, workflow_ids)
    refuse_invalid_structure(db, body)
    nodes = [
        NodeSpec(
            n.key,
            n.kind,
            n.version_id if n.kind != WorkflowNodeKind.approval else None,
            n.input_schema,
            n.output_schema,
            n.input_template,
            **(
                {
                    "prompt_template": n.prompt_template,
                    "approver_role": n.approver_role or Role.editor.value,
                    "expiry_days": n.expiry_days,
                }
                if n.kind == WorkflowNodeKind.approval
                else {}
            ),
        )
        for n in body.nodes
    ]
    return nodes, edges


def approval_node_errors(body: WorkflowIn) -> list[tuple[str, str]]:
    """Why the request's approval node fields can't be saved, by item: an approval node needs a
    prompt, pins nothing, and gives a fixed output (`approvals.OUTCOME_SCHEMA`), so declares no
    output schema; its prompt's placeholders name only predecessors. Other nodes have none of
    its fields."""
    sources: dict[str, list[str]] = {n.key: [] for n in body.nodes}
    for e in body.edges:
        sources.setdefault(e.target, []).append(e.source)
    errors: list[tuple[str, str]] = []
    for n in body.nodes:
        item = f"nodes.{n.key}"
        if n.kind != WorkflowNodeKind.approval:
            for name in ("prompt_template", "approver_role", "expiry_days"):
                if getattr(n, name) is not None:
                    errors.append((f"{item}.{name}", "Only an approval node has this"))
            continue
        if n.version_id is not None:
            errors.append((f"{item}.version_id", "An approval node runs no agent or workflow"))
        if n.output_schema is not None:
            errors.append(
                (
                    f"{item}.output_schema",
                    "An approval node's output is its decision: approved, note, decided_by"
                    " and status",
                )
            )
        if not (n.prompt_template or "").strip():
            errors.append((f"{item}.prompt_template", "Write the prompt its approvers see"))
        else:
            errors += [
                (f"{item}.prompt_template", e)
                for e in template_errors(n.prompt_template, sources.get(n.key, []))
            ]
    return errors


class StructureRefusal(dict):
    """A 422's detail for invalid schemas, templates or conditions: a message, and each error
    with the item it's about. Read as text, its message."""

    def __str__(self) -> str:
        return self["message"]


def refuse_invalid_structure(db: Session, body: WorkflowIn) -> None:
    """Refuse the request's schemas, templates and conditions if any is invalid: a 422 naming
    each invalid item (`aios.workflows.structured.check_workflow`). A workflow node's conditions
    read its own output schema, or else its pinned version's."""
    pinned = {
        n.version_id
        for n in body.nodes
        if n.kind == WorkflowNodeKind.workflow and n.output_schema is None
    }
    inner = (
        dict(
            db.execute(
                select(WorkflowVersion.id, WorkflowVersion.output_schema).where(
                    WorkflowVersion.id.in_(pinned)
                )
            ).all()
        )
        if pinned
        else {}
    )
    errors = approval_node_errors(body) + check_workflow(
        [
            NodeDeclared(
                n.key,
                n.input_schema,
                approvals.OUTCOME_SCHEMA
                if n.kind == WorkflowNodeKind.approval
                else n.output_schema
                if n.output_schema is not None
                else inner.get(n.version_id),
                n.input_template,
            )
            for n in body.nodes
        ],
        [(e.source, e.target, e.condition) for e in body.edges],
        body.input_schema,
        body.output_schema,
    )
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            StructureRefusal(
                message="; ".join(f"{item}: {message}" for item, message in errors),
                errors=[{"item": item, "message": message} for item, message in errors],
            ),
        )


def conditions_of(body: WorkflowIn) -> dict[tuple[str, str], Any]:
    return {(e.source, e.target): e.condition for e in body.edges if e.condition is not None}


def lock_graph(
    db: Session, nodes: Sequence[NodeSpec], workflow: Workflow | None
) -> set[WorkflowNodeKind]:
    """Row-lock what saving `workflow` with these nodes depends on; return the node kinds whose
    agent or workflow has been archived.

    Locks the agents the nodes run, then the workflows they run together with `workflow` itself
    (unless it's new), so a concurrent archive of any of them waits for this save's commit, or
    commits first and is seen here. All the workflows are locked in one id-ordered pass, so two
    saves of workflows that run each other can't deadlock; see `lock_live` for the lock order.
    `workflow` itself may be archived: it can still be edited.
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
    archived_kinds = set()
    if lock_live(db, Agent, agent_ids) != agent_ids:
        archived_kinds.add(WorkflowNodeKind.agent)
    live = lock_live(db, Workflow, workflow_ids | own)
    if workflow_ids - live:
        archived_kinds.add(WorkflowNodeKind.workflow)
    return archived_kinds


def lock_new_graph(db: Session, nodes: Sequence[NodeSpec], workflow: Workflow | None) -> None:
    """`lock_graph` for a graph from the request: an archived target is an unknown version."""
    archived_kinds = lock_graph(db, nodes, workflow)
    if WorkflowNodeKind.agent in archived_kinds:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown agent version")
    if WorkflowNodeKind.workflow in archived_kinds:
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
    if node.kind == WorkflowNodeKind.approval:
        return WorkflowNodeOut(
            key=node.key,
            kind=node.kind,
            version_id=None,
            target_id=None,
            name="Approval",
            version=None,
            latest_version=None,
            latest_version_id=None,
            input_schema=node.input_schema,
            input_template=node.input_template,
            prompt_template=node.prompt_template,
            approver_role=node.approver_role,
            expiry_days=node.expiry_days,
        )
    if node.kind == WorkflowNodeKind.agent:
        pinned: Any = node.agent_version
        target: Any = pinned.agent
    else:
        pinned = node.subworkflow_version
        target = pinned.workflow
    latest = target.current_version
    return WorkflowNodeOut(
        key=node.key,
        kind=node.kind,
        version_id=pinned.id,
        target_id=target.id,
        name=target.name,
        version=pinned.version,
        latest_version=latest.version if latest else None,
        latest_version_id=latest.id if latest else None,
        input_schema=node.input_schema,
        output_schema=node.output_schema,
        input_template=node.input_template,
    )


def unrunnable(store: Session, version: WorkflowVersion, org: ToolOrganisation) -> str | None:
    """Why a run of the version can't start in `org` because of an agent it runs, at any depth,
    whose tools can't be built there (as `load_graph` would refuse it), the first by name; None
    if none. One query, however many nodes and levels."""
    reach = reachable_versions(version.id)
    rows = store.execute(
        select(Agent.name, AgentVersion.version, AgentVersionTool.tool_key, AgentVersionTool.config)
        .select_from(WorkflowNode)
        .join(AgentVersion, AgentVersion.id == WorkflowNode.agent_version_id)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(AgentVersionTool, AgentVersionTool.agent_version_id == AgentVersion.id)
        .where(WorkflowNode.workflow_version_id.in_(select(reach.c.id)))
        .order_by(Agent.name, AgentVersion.version, AgentVersionTool.position)
    ).all()
    tools: dict[tuple[str, int], dict[str, dict]] = {}
    for name, number, key, config in rows:
        tools.setdefault((name, number), {})[key] = config
    for (name, number), pinned in tools.items():
        if why := unusable_tools(pinned.items(), org):
            return f"This version can't run: Agent {name} v{number} can't run: {why}"
    return None


def version_out(
    db: Session, org_id: int, version: WorkflowVersion, *, runnable: bool = True
) -> WorkflowVersionOut:
    """The version (in the organisation `org_id`'s store), with who saved it read through the
    platform session `db`, and, with `runnable`, whether a run of it can start (not for a run's
    page, which polls a run already going and needn't read its agents' tools)."""
    store = object_session(version)
    saved_by = people(db, org_id, [version.created_by]).get(version.created_by)
    return WorkflowVersionOut(
        id=version.id,
        version=version.version,
        note=version.note,
        created_at=version.created_at,
        created_by=saved_by,
        nodes=[node_out(n) for n in version.nodes],
        edges=[
            WorkflowEdgeOut(source=e.source, target=e.target, condition=e.condition)
            for e in version.edges
        ],
        input_schema=version.input_schema,
        output_schema=version.output_schema,
        unrunnable=unrunnable(store, version, ToolOrganisation(org_id, store))
        if runnable
        else None,
    )


def workflow_out(db: Session, org_id: int, store: Session, workflow: Workflow) -> WorkflowOut:
    """The workflow (in the organisation `org_id`'s store, `store`), with who made, changed and
    archived it read through the platform session `db`."""
    known = people(
        db,
        org_id,
        [workflow.created_by, workflow.updated_by, workflow.archived_by],
    )
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        current_version=version_out(db, org_id, load_version(store, workflow.current_version_id)),
        layout=workflow.layout,
        invocable_by=workflow.invocable_by,
        limits=RunLimits.model_validate(workflow.limits or {}),
        limits_in_force=RunLimits.model_validate(
            limits.in_force(organisation_limits(db, org_id), workflow.limits)
        ),
        **provenance(db, workflow, known),
        **archived(workflow, known),
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


def contained_workflows(store: Session) -> dict[int, set[int]]:
    """For each of the organisation's workflows (in its store), the workflows its current version
    runs, at any depth, in one recursive query (and so ending, as in
    `refuse_self_containment`)."""
    pinned = aliased(WorkflowVersion)
    reach = (
        select(Workflow.id.label("root_id"), pinned.id.label("version_id"), pinned.workflow_id)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == Workflow.current_version_id)
        .join(pinned, pinned.id == WorkflowNode.subworkflow_version_id)
        .cte("reach", recursive=True)
    )
    reach = reach.union(
        select(reach.c.root_id, pinned.id, pinned.workflow_id)
        .join(WorkflowNode, WorkflowNode.workflow_version_id == reach.c.version_id)
        .join(pinned, pinned.id == WorkflowNode.subworkflow_version_id)
    )
    contained: dict[int, set[int]] = {}
    for root_id, workflow_id in store.execute(
        select(reach.c.root_id, reach.c.workflow_id).distinct()
    ):
        contained.setdefault(root_id, set()).add(workflow_id)
    return contained


@router.get("/workflows", response_model=list[WorkflowSummary], dependencies=[allow(Action.read)])
def list_workflows(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    archived_only: Annotated[bool, Query(alias="archived")] = False,
):
    """The organisation's workflows that aren't archived, by name; with `archived`, the archived
    ones instead."""
    rows = store.execute(
        select(
            Workflow, WorkflowVersion.version, WorkflowVersion.id, node_count(WorkflowVersion.id)
        )
        .join(WorkflowVersion, WorkflowVersion.id == Workflow.current_version_id)
        .where(
            Workflow.archived_at.is_not(None) if archived_only else Workflow.archived_at.is_(None)
        )
        .order_by(Workflow.name)
    ).all()
    contained = contained_workflows(store)
    known = people(
        db, org.id, [i for w, *_ in rows for i in (w.created_by, w.updated_by, w.archived_by)]
    )
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
            **provenance(db, w, known),
            **archived(w, known),
        )
        for w, version, version_id, nodes in rows
    ]


@router.post(
    "/workflows",
    response_model=WorkflowOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.build)],
)
def create_workflow(
    body: WorkflowIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    nodes, edges = resolve_graph(store, body)
    lock_new_graph(store, nodes, None)
    workflow = Workflow(
        **built_by(user.id),
        name=body.name,
        description=body.description,
        layout={},
        invocable_by=InvocableBy.everyone,
        limits={},
    )
    apply_guards(db, org.id, role, workflow, body.invocable_by, body.limits)
    apply_layout(workflow, body)
    store.add(workflow)
    with unique_name(store, "a workflow", WORKFLOW_NAME_INDEX):
        save_workflow_version(
            store,
            workflow,
            nodes=nodes,
            edges=edges,
            note=body.note,
            user_id=user.id,
            conditions=conditions_of(body),
            input_schema=body.input_schema,
            output_schema=body.output_schema,
        )
        audit.configured(store, user.id, "workflow", workflow, "created")
        store.commit()
    return workflow_out(db, org.id, store, workflow)


@router.get(
    "/workflows/{workflow_id}", response_model=WorkflowOut, dependencies=[allow(Action.read)]
)
def get_workflow(workflow_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    return workflow_out(db, org.id, store, get_org_workflow(store, workflow_id))


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


@router.put(
    "/workflows/{workflow_id}", response_model=WorkflowOut, dependencies=[allow(Action.build)]
)
def update_workflow(
    workflow_id: DbId,
    body: WorkflowIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    role: CurrentRole,
    db: DB,
    store: CurrentStore,
):
    """Rename/describe the workflow; a changed graph creates a new version. Who may invoke it and
    its caps are the workflow's, not versioned: only admins raise a cap above the
    organisation's (403)."""
    workflow = get_org_workflow(store, workflow_id)
    # Before checking the graph: an edit of an older version may be invalid only because of what
    # has changed since, and reloading is what fixes it.
    refuse_stale_edit(store, workflow, body.base_version)
    nodes, edges = resolve_graph(store, body, workflow)
    lock_new_graph(store, nodes, workflow)
    # Another save may have changed the layout, or made a new version, while we waited for the
    # lock; prune that layout, and compare against that version.
    store.refresh(workflow, ["layout", "current_version_id"])
    refuse_stale_edit(store, workflow, body.base_version)
    workflow.name = body.name
    workflow.description = body.description
    workflow.updated_by = user.id
    apply_guards(db, org.id, role, workflow, body.invocable_by, body.limits)
    apply_layout(workflow, body)
    with unique_name(store, "a workflow", WORKFLOW_NAME_INDEX):
        save_workflow_version(
            store,
            workflow,
            nodes=nodes,
            edges=edges,
            note=body.note,
            user_id=user.id,
            conditions=conditions_of(body),
            input_schema=body.input_schema,
            output_schema=body.output_schema,
        )
        audit.configured(store, user.id, "workflow", workflow, "updated")
        store.commit()
    return workflow_out(db, org.id, store, workflow)


@router.post(
    "/workflows/{workflow_id}/archive",
    response_model=WorkflowOut,
    dependencies=[allow(Action.build)],
)
def archive_workflow(
    workflow_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Archive the workflow: no new run starts on it, by hand, by schedule, by mention or nested
    in another workflow, and its schedules are paused. Never refused, whatever uses it
    (`GET .../used-by`); runs already going finish, and nothing is lost. Archiving an archived
    workflow changes nothing."""
    workflow = get_org_workflow(store, workflow_id)
    # Starting a run share-locks the workflow and checks it isn't archived
    # (`lock_live_workflow`), so one being started is waited for here, and goes on as already
    # going. Saving a workflow that nests it locks it too (`lock_graph`).
    lock_workflow(store, workflow)
    if workflow.archived_at is None:
        workflow.archived_at = func.now()
        workflow.archived_by = user.id
        # Its schedules are paused with it (see `aios.api.agents.archive_agent`).
        paused = disable_triggers(store, Trigger.workflow_id == workflow.id, user.id)
        audit.configured(store, user.id, "workflow", workflow, "archived")
        store.commit()
        unindex(store, paused)
    return workflow_out(db, org.id, store, workflow)


@router.post(
    "/workflows/{workflow_id}/unarchive",
    response_model=WorkflowOut,
    dependencies=[allow(Action.build)],
)
def unarchive_workflow(
    workflow_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Bring the workflow back. Its schedules stay paused until someone resumes them."""
    workflow = get_org_workflow(store, workflow_id)
    lock_workflow(store, workflow)
    if workflow.archived_at is not None:
        audit.configured(store, user.id, "workflow", workflow, "unarchived")
    workflow.archived_at = None
    workflow.archived_by = None
    store.commit()
    return workflow_out(db, org.id, store, workflow)


@router.get(
    "/workflows/{workflow_id}/used-by",
    response_model=used_by.WorkflowUsedBy,
    dependencies=[allow(Action.read)],
)
def get_workflow_used_by(
    workflow_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """What uses the workflow, for the archive confirmation to show: read-only, as archiving is
    never refused."""
    return used_by.workflow(store, get_org_workflow(store, workflow_id), user.id)


@router.get(
    "/workflows/{workflow_id}/versions",
    response_model=list[WorkflowVersionSummary],
    dependencies=[allow(Action.read)],
)
def list_workflow_versions(
    workflow_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    workflow = get_org_workflow(store, workflow_id)
    rows = store.execute(
        select(WorkflowVersion, node_count(WorkflowVersion.id))
        .where(WorkflowVersion.workflow_id == workflow.id)
        .order_by(WorkflowVersion.version.desc())
    ).all()
    known = people(db, org.id, [v.created_by for v, _ in rows])
    return [
        WorkflowVersionSummary(
            id=v.id,
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by=known.get(v.created_by),
            node_count=nodes,
        )
        for v, nodes in rows
    ]


@router.get(
    "/workflows/{workflow_id}/versions/{number}",
    response_model=WorkflowVersionOut,
    dependencies=[allow(Action.read)],
)
def get_workflow_version_detail(
    workflow_id: DbId, number: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    workflow = get_org_workflow(store, workflow_id)
    version = load_version(store, get_workflow_version(store, workflow, number).id)
    return version_out(db, org.id, version)


@router.post(
    "/workflows/{workflow_id}/versions/{number}/restore",
    response_model=WorkflowOut,
    dependencies=[allow(Action.build)],
)
def restore_workflow_version(
    workflow_id: DbId,
    number: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Make an old version's graph current again, as a new version (numbers only go up)."""
    workflow = get_org_workflow(store, workflow_id)
    old = get_workflow_version(store, workflow, number)
    # The versions its nodes pin still exist (nothing is deleted), but the agents and workflows
    # they belong to may have been archived since.
    nodes = [
        NodeSpec(
            n.key,
            n.kind,
            n.agent_version_id or n.subworkflow_version_id,
            n.input_schema,
            n.output_schema,
            n.input_template,
            n.prompt_template,
            n.approver_role,
            n.expiry_days,
        )
        for n in old.nodes
    ]
    edges = [(e.source, e.target) for e in old.edges]
    conditions = {(e.source, e.target): e.condition for e in old.edges}
    # The old version passed this check when saved and pins never change, so this only refuses
    # versions saved before the check covered nesting through other workflows.
    refuse_self_containment(
        store, workflow, {n.subworkflow_version_id for n in old.nodes if n.subworkflow_version_id}
    )
    archived_kinds = lock_graph(store, nodes, workflow)
    if archived_kinds:
        what = " and ".join(
            f"{'an' if kind == WorkflowNodeKind.agent else 'a'} {kind}"
            for kind in sorted(archived_kinds)
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"That version runs {what} that has since been archived."
        )
    # Keep the positions of the nodes it shares with the current graph, and drop the rest.
    keys = {n.key for n in old.nodes}
    workflow.layout = {key: p for key, p in workflow.layout.items() if key in keys}
    workflow.updated_by = user.id
    save_workflow_version(
        store,
        workflow,
        nodes=nodes,
        edges=edges,
        note=f"Restored from v{old.version}",
        user_id=user.id,
        conditions=conditions,
        input_schema=old.input_schema,
        output_schema=old.output_schema,
    )
    audit.configured(store, user.id, "workflow", workflow, "version_restored", version=old.version)
    store.commit()
    return workflow_out(db, org.id, store, workflow)


# --- Runs ---------------------------------------------------------------------------------------


class WorkflowRunIn(BaseModel):
    # Given to the nodes with no incoming edge, as the user's message to an agent. For a version
    # with an input schema, JSON (`input_json`), or text read as JSON if it is.
    input: str | None = Field(default=None, min_length=1, pattern=r"\S")
    input_json: Any = None

    @model_validator(mode="after")
    def _given(self) -> "WorkflowRunIn":
        if self.input is None and self.input_json is None:
            raise ValueError("Give the run an input")
        return self


class WorkflowRunSummary(BaseModel):
    id: int
    workflow_id: int
    workflow_version_id: int
    workflow_version: int
    status: WorkflowRunStatus
    input: str
    # Its input and output as JSON: the input when the version has an input schema; the output
    # one final node's, or an object of several's by node.
    input_json: Any = None
    output_json: Any = None
    # Why a run that succeeded has no output: "No final step ran".
    output_note: str | None = None
    output: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # While it waits, since when; and how long it waited before (seconds), which doesn't count
    # towards its time limit.
    waiting_since: datetime | None
    waited_seconds: float
    # The chat a mention in it started it from, while the chat exists.
    chat_id: int | None
    chat_name: str | None
    # The member who started it: by hand, or by mentioning the workflow in a chat. Null for a
    # schedule's run (`trigger_id` says which, while it exists), and once that user is deleted.
    created_by: UserOut | None
    trigger_id: int | None


class WorkflowRunStepOut(BaseModel):
    id: int
    # Node keys from the run's workflow down to this node.
    path: list[str]
    kind: WorkflowNodeKind
    status: WorkflowStepStatus
    input: str
    output: str | None
    error: str | None
    # Its input and output as JSON, when structured; and, once it succeeded, how its outgoing
    # edges' conditions came out, by target (only those with one).
    input_json: Any = None
    output_json: Any = None
    conditions: dict[str, bool] | None = None
    # An agent node's conversation, on the agent's run page; None for workflow nodes, and once
    # the agent run has been deleted (with its agent).
    agent_id: int | None
    agent_run_id: int | None
    # While an agent node waits: the schema change requests it waits on (one per call), oldest
    # first; an approval node's, its approval.
    waiting_on_request_ids: list[uuid.UUID]
    # The approval it waits on, or an approval node's once decided (`aios.approvals`).
    approval_id: uuid.UUID | None = None
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
    # None only while one is being made.
    current_version: int | None


class WorkflowRunOut(WorkflowRunSummary):
    # The pinned version's graph.
    version: WorkflowVersionOut
    # In the order they started.
    steps: list[WorkflowRunStepOut]
    # While it waits: the pins, at any depth, it would resume on that are no longer current.
    stale_pins: list[StalePin]
    # Why it ran (`aios.invocations`).
    invocation: InvocationRecordOut | None = None


def stale_pins(db: Session, version_id: int) -> list[StalePin]:
    """The versions a run of a workflow version pins that are no longer current: the version
    itself first (path `[]`), then its nodes' at any depth, in node order, each subworkflow's
    after its node. Two queries, whatever the graph, reading only versions and names (a waiting
    run's page polls this)."""
    pinned_agent, current_agent = aliased(AgentVersion), aliased(AgentVersion)
    pinned_flow, current_flow = aliased(WorkflowVersion), aliased(WorkflowVersion)
    top = db.execute(
        select(
            pinned_flow.version.label("pinned"),
            Workflow.name,
            current_flow.version.label("current"),
        )
        .join(Workflow, Workflow.id == pinned_flow.workflow_id)
        .outerjoin(current_flow, current_flow.id == Workflow.current_version_id)
        .where(pinned_flow.id == version_id)
    ).one()
    rows = db.execute(
        select(
            WorkflowNode.workflow_version_id,
            WorkflowNode.key,
            WorkflowNode.kind,
            WorkflowNode.subworkflow_version_id,
            func.coalesce(pinned_agent.version, pinned_flow.version).label("pinned"),
            func.coalesce(Agent.name, Workflow.name).label("name"),
            func.coalesce(current_agent.version, current_flow.version).label("current"),
        )
        .outerjoin(pinned_agent, pinned_agent.id == WorkflowNode.agent_version_id)
        .outerjoin(Agent, Agent.id == pinned_agent.agent_id)
        .outerjoin(current_agent, current_agent.id == Agent.current_version_id)
        .outerjoin(pinned_flow, pinned_flow.id == WorkflowNode.subworkflow_version_id)
        .outerjoin(Workflow, Workflow.id == pinned_flow.workflow_id)
        .outerjoin(current_flow, current_flow.id == Workflow.current_version_id)
        .where(WorkflowNode.workflow_version_id.in_(select(reachable_versions(version_id).c.id)))
        .order_by(WorkflowNode.workflow_version_id, WorkflowNode.position)
    ).all()
    nodes: dict[int, list] = {}
    for row in rows:
        nodes.setdefault(row.workflow_version_id, []).append(row)
    stale: list[StalePin] = []

    def check(path: list[str], kind: WorkflowNodeKind, pin) -> None:
        # Without a current version (only while one is being made), nothing to compare.
        if pin.current is not None and pin.current != pin.pinned:
            stale.append(
                StalePin(
                    path=path,
                    kind=kind,
                    name=pin.name,
                    pinned_version=pin.pinned,
                    current_version=pin.current,
                )
            )

    def walk(version_id: int, path: list[str]) -> None:
        for node in nodes.get(version_id, []):
            check([*path, node.key], node.kind, node)
            if node.subworkflow_version_id is not None:
                walk(node.subworkflow_version_id, [*path, node.key])

    check([], WorkflowNodeKind.workflow, top)
    walk(version_id, [])
    return stale


def waiting_requests(store: Session, steps: Sequence[WorkflowRunStep]) -> list[SchemaChangeRequest]:
    """The requests the waiting steps among `steps` (the store's) wait on, oldest first: those
    their agent runs made that they haven't been given, and each one's recorded request (its
    agent run may have gone)."""
    waiting = [s for s in steps if s.status == WorkflowStepStatus.waiting]
    agent_run_ids = [s.agent_run_id for s in waiting if s.agent_run_id is not None]
    recorded = [s.approval_id for s in waiting if s.approval_id is not None]
    if not waiting:
        return []
    return list(
        store.scalars(
            select(SchemaChangeRequest)
            .where(
                or_(
                    SchemaChangeRequest.requested_by_run.in_(agent_run_ids),
                    SchemaChangeRequest.id.in_(recorded),
                ),
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status != SchemaChangeStatus.cancelled,
            )
            .order_by(SchemaChangeRequest.created_at, SchemaChangeRequest.id)
        )
    )


def waits_on(step: WorkflowRunStep, requests: list[SchemaChangeRequest]) -> list[uuid.UUID]:
    if step.status != WorkflowStepStatus.waiting:
        return []
    return [
        r.id
        for r in requests
        if r.id == step.approval_id
        or (step.agent_run_id is not None and r.requested_by_run == step.agent_run_id)
    ]


def get_org_workflow_run(store: Session, workflow_id: int, run_id: int) -> WorkflowRun:
    """A run of the organisation's workflow, archived or not, in its store (`store`); a 404
    otherwise, as if it didn't exist. Every member reads and deletes every run of the
    organisation's, whoever started it and however."""
    run = store.get(WorkflowRun, run_id)
    if run is None or run.workflow_id != workflow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def workflow_run_summary_query():
    """The store's workflow runs, as summaries' columns (`workflow_run_summaries` adds the
    rest)."""
    return select(
        WorkflowRun.id,
        WorkflowRun.workflow_id,
        WorkflowRun.workflow_version_id,
        WorkflowVersion.version.label("workflow_version"),
        WorkflowRun.status,
        WorkflowRun.input,
        WorkflowRun.input_json,
        WorkflowRun.output,
        WorkflowRun.output_json,
        WorkflowRun.error,
        WorkflowRun.created_at,
        WorkflowRun.started_at,
        WorkflowRun.finished_at,
        WorkflowRun.waiting_since,
        WorkflowRun.waited_seconds,
        # As the user, by `workflow_run_summaries` (`provenance.started_by`).
        WorkflowRun.created_by.label("created_by_id"),
        WorkflowRun.trigger_id,
    ).join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)


def workflow_run_summaries(
    db: Session, organisation_id: int, store: Session, rows: Sequence[Row]
) -> list[WorkflowRunSummary]:
    """Rows of `workflow_run_summary_query` for the organisation's runs (in its store, `store`)
    as summaries: with the chats that started them, and who started each."""
    chats = run_chats(store, Invocation.workflow_run_id, [r.id for r in rows])
    return started_by(
        db,
        organisation_id,
        [
            {
                **row._mapping,
                "output_note": NO_FINAL_STEP
                if row.status == WorkflowRunStatus.succeeded and row.output is None
                else None,
                "chat_id": chats.get(row.id, (None, None))[0],
                "chat_name": chats.get(row.id, (None, None))[1],
            }
            for row in rows
        ],
        WorkflowRunSummary,
    )


def workflow_run_summary(
    db: Session, organisation_id: int, store: Session, run_id: int
) -> WorkflowRunSummary:
    row = store.execute(workflow_run_summary_query().where(WorkflowRun.id == run_id)).one()
    return workflow_run_summaries(db, organisation_id, store, [row])[0]


@router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[WorkflowRunSummary],
    dependencies=[allow(Action.read)],
)
def list_workflow_runs(workflow_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    workflow = get_org_workflow(store, workflow_id)
    theirs = WorkflowRun.workflow_id == workflow.id
    rows = store.execute(
        workflow_run_summary_query()
        .where(theirs)
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
    ).all()
    return workflow_run_summaries(db, org.id, store, rows)


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=WorkflowRunSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def create_workflow_run(
    workflow_id: DbId,
    body: WorkflowRunIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    background: BackgroundTasks,
):
    """Start a run of the workflow's current version. The run service runs it, node by node
    (`aios.workflows.advance`)."""
    workflow = get_org_workflow(store, workflow_id)
    run = new_workflow_run(
        store, org.id, workflow, body.input, input_json=body.input_json, created_by=user.id
    )
    work = queue_workflow_run(store, run, org.id)
    background.add_task(run_service.wake, work.lane)
    return workflow_run_summary(db, org.id, store, run.id)


def new_workflow_run(
    store: Session,
    organisation_id: int,
    workflow: Workflow,
    input: str | None,
    *,
    input_json: Any = None,
    created_by: int | None,
    trigger_id: int | None = None,
    by_mention: bool = False,
    event: Event | None = None,
) -> WorkflowRun:
    """Add (and flush, not commit) a run of the workflow's current version to the store of the
    organisation `organisation_id` (`store`, where the workflow is), given `input`, started by
    the member `created_by`; it runs for the workflow's organisation, not for them
    (`aios.workflows`), as do its agent nodes' runs, which it passes `created_by` on to.

    Raises `HTTPException` if it can't run: 409 if it, or an agent or workflow in its graph at
    any depth, is archived ("The workflow <name> is archived.") or an agent version in it can't
    run; 422 if it has no nodes. Commit with `queue_workflow_run`; the share lock taken here
    keeps a concurrent archive waiting until the run counts as already going. Also how a
    schedule starts one (`aios.scheduler`), passing its `trigger_id` and no member.

    The input is text (`input`), or JSON (`input_json`, read in place of the text). A version
    with an input schema has it checked, the text read as JSON if it is: a 422 with the
    validation errors if it doesn't fit (`run_input`).

    Its invocation is made with it (`invocations.for_run`): a schedule's, an event trigger's
    (with the `event` it fired on), or one started by hand by `created_by`, running from when
    the run is queued. A mention's run (`by_mention`) has its
    mention's, which the caller links.
    """
    lock_live_workflow(store, workflow)
    version = workflow.current_version
    if not version.nodes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This workflow has no nodes")
    # Checked now, at every depth, so a version that can't run doesn't leave a run that fails.
    if refusal := archived_in_graph(store, version.id):
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    try:
        load_graph(store, version.id, organisation_id)
    except RunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    text, value = run_input(version.input_schema, input, input_json)
    # Pin the version: later edits to the workflow don't change this run.
    run = WorkflowRun(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        input=text,
        input_json=value,
        created_by=created_by,
        trigger_id=trigger_id,
    )
    store.add(run)
    store.flush()
    if not by_mention:
        invocation = invocations.for_run(
            store,
            TriggerTargetKind.workflow,
            workflow.id,
            requested_by=created_by,
            trigger_id=trigger_id,
            event=event,
            status=InvocationStatus.running,
        )
        invocation.workflow_run_id = run.id
        store.flush()
    return run


NO_FINAL_STEP = "No final step ran"


def run_input(schema: Any, text: str | None, value: Any = None) -> tuple[str, Any]:
    """A run's input as text and as JSON (None without an input schema). With a schema, JSON
    given is the input, else the text read as JSON if it is (otherwise the text itself); a 422
    if it doesn't fit, with each validation error's path and message. Without one, JSON given
    is the input as text."""
    if schema is None:
        if text is None:
            text = compact(value)
        return text, None
    if value is None:
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            value = text
    errors = schema_errors(value, schema)
    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            StructureRefusal(
                message=f"The input doesn't fit the workflow's input schema: {describe(errors)}",
                errors=[{"path": path, "message": message} for path, message in errors],
            ),
        )
    return compact(value), value


def enqueue_run(work: run_service.Work) -> None:
    """Enqueue a run's first advance on the run service, in the platform database."""
    with SessionLocal() as db:
        run_service.enqueue(db, [work])
        db.commit()


def queue_workflow_run(store: Session, run: WorkflowRun, organisation_id: int) -> run_service.Work:
    """Commit the run in its store (with whatever else is pending there: what started it, a
    schedule's or a chat mention's link to it, before the run service can end the run and look
    for it) and enqueue its first advance on the run service; the item, for the caller to wake
    the run service for once it's done. If it can't be queued, delete the run (what links to it
    stops naming it) and raise a 503."""
    store.commit()
    store_id = store_id_of(store)
    work = advance_work(organisation_id, store_id, run.id)
    try:
        enqueue_run(work)
    except Exception as exc:
        # Nothing would ever pick up a committed-but-unqueued run; drop it so the client can
        # simply retry without leaving one stuck in `queued`: with the invocation made for it (a
        # mention's stays, and is failed by its caller).
        store.execute(
            delete(Invocation).where(
                Invocation.workflow_run_id == run.id, Invocation.chat_id.is_(None)
            )
        )
        store.delete(run)
        store.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not queue the run; please try again shortly.",
        ) from exc
    return work


class WorkflowRunPageOut(WorkflowRunOut):
    """The run as its page reads it."""

    # The workflow's name, so the page needn't read the workflow as well.
    workflow_name: str


@router.get(
    "/workflows/{workflow_id}/runs/{run_id}",
    response_model=WorkflowRunPageOut,
    dependencies=[allow(Action.read)],
)
def get_workflow_run(
    workflow_id: DbId, run_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    run = get_org_workflow_run(store, workflow_id, run_id)
    name = get_org_workflow(store, run.workflow_id).name
    out = workflow_run_out(db, org.id, store, run.id)
    return WorkflowRunPageOut(**out.model_dump(), workflow_name=name)


def workflow_run_out(
    db: Session, organisation_id: int, store: Session, run_id: int
) -> WorkflowRunOut:
    """The run as its page shows it: its summary, pinned graph and steps, from the
    organisation's store (`store`). Also what a chat's card of it draws (`aios.api.chats`)."""
    summary = workflow_run_summary(db, organisation_id, store, run_id)
    steps = store.execute(
        select(WorkflowRunStep, AgentRun.agent_id)
        .outerjoin(AgentRun, AgentRun.id == WorkflowRunStep.agent_run_id)
        .where(WorkflowRunStep.run_id == run_id)
        .order_by(WorkflowRunStep.id)
    ).all()
    waits = waiting_requests(store, [step for step, _ in steps])
    steps_out = [
        WorkflowRunStepOut(
            id=step.id,
            path=step.path,
            kind=step.kind,
            status=step.status,
            input=step.input,
            output=step.output,
            error=step.error,
            input_json=step.input_json,
            output_json=step.output_json,
            conditions=step.conditions,
            agent_id=agent_id,
            agent_run_id=step.agent_run_id,
            waiting_on_request_ids=waits_on(step, waits),
            approval_id=step.approval_id,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )
        for step, agent_id in steps
    ]
    version = load_version(store, summary.workflow_version_id)
    stale = (
        stale_pins(store, summary.workflow_version_id)
        if summary.status == WorkflowRunStatus.waiting
        else []
    )
    invocation = invocations.of_workflow_run(store, run_id)
    return WorkflowRunOut(
        **summary.model_dump(),
        version=version_out(db, organisation_id, version, runnable=False),
        steps=steps_out,
        stale_pins=stale,
        invocation=None if invocation is None else InvocationRecordOut.model_validate(invocation),
    )


@router.get(
    "/workflows/{workflow_id}/runs/{run_id}/subworkflow",
    response_model=WorkflowVersionOut,
    dependencies=[allow(Action.read)],
)
def get_run_subworkflow(
    workflow_id: DbId,
    run_id: DbId,
    path: Annotated[list[str], Query(min_length=1)],
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """The version the run's graph pinned for the workflow node at `path` (node keys, outermost
    first): what its page draws on opening a subworkflow, as the run pinned it (the subworkflow
    may have changed since, or been deleted)."""
    run = get_org_workflow_run(store, workflow_id, run_id)
    version_id = run.workflow_version_id
    for key in path:
        node = store.get(WorkflowNode, (version_id, key))
        if node is None or node.kind != WorkflowNodeKind.workflow:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        version_id = node.subworkflow_version_id
    return version_out(db, org.id, load_version(store, version_id), runnable=False)


@router.delete(
    "/workflows/{workflow_id}/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_workflow_run(
    workflow_id: DbId,
    run_id: DbId,
    store: CurrentStore,
    background: BackgroundTasks,
) -> None:
    """Delete the run, and the agent runs its agent nodes held their conversations in."""
    run = get_org_workflow_run(store, workflow_id, run_id)
    # Locked before its agent runs are read: an advance starting a node meanwhile records its
    # step and agent run under this lock, so they're either seen here or never made once the
    # run has gone (`aios.workflows.advance`).
    still_going = store.scalar(select(alive()).where(WorkflowRun.id == run.id).with_for_update())
    # Its agent turns would carry on regardless. A queued run can go: an advance finds nothing
    # to start for a run that's gone; so can a waiting one, which holds nothing.
    if still_going:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This run is still going; wait for it to finish."
        )
    steps = store.execute(
        select(WorkflowRunStep.agent_run_id, WorkflowRunStep.approval_id).where(
            WorkflowRunStep.run_id == run.id
        )
    ).all()
    agent_run_ids = [s.agent_run_id for s in steps if s.agent_run_id is not None]
    request_ids = [s.approval_id for s in steps if s.approval_id is not None]
    sandboxes = run_sandboxes(store, agent_run_ids)
    # Their conversations go with them. Locked first: a request being recorded for one
    # meanwhile is waited for (`schema_changes.lock_run`), and then seen.
    schema_changes.lock_run(store, agent_run_ids)
    # A pending request can't be approved any more.
    cancelled = close_requests(store, agent_run_ids, request_ids)
    # A chat that started it and is still following it is told it's gone.
    invocations.fail_deleted_run(store, run.id)
    checkpoints.delete_threads(store, agent_run_ids)
    store.execute(delete(AgentRun).where(AgentRun.id.in_(agent_run_ids)))  # and their sessions
    store.delete(run)  # and its steps
    store.commit()
    # Then what the platform says of them.
    approvals.clear_notifications_of(cancelled)
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


@router.post(
    "/workflows/{workflow_id}/runs/{run_id}/cancel",
    response_model=WorkflowRunPageOut,
    dependencies=[allow(Action.post)],
)
def cancel_workflow_run(
    workflow_id: DbId,
    run_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Cancel a run that hasn't ended: it fails ("Cancelled by <name>."), with its steps still
    going, and the approvals it waits on are cancelled, their notifications cleared
    (`aios.approvals`). A 409 if it has ended."""
    run = get_org_workflow_run(store, workflow_id, run_id)
    locked = store.scalar(select(WorkflowRun).where(WorkflowRun.id == run.id).with_for_update())
    if locked is None or locked.status not in NOT_ENDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "This run has already ended.")
    # A waiting run is ended as a running one would be, adding the time it waited.
    store.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == WorkflowRunStatus.waiting)
        .values(status=WorkflowRunStatus.running)
    )
    name = people(db, org.id, [user.id]).get(user.id)
    error = f"Cancelled by {name.name}." if name is not None else "Cancelled."
    ended = _end_runs(store, [WorkflowRun.id == run.id], WorkflowRunStatus.failed, None, error)
    audit.record(
        store,
        audit.person(user.id),
        "workflow_run.cancelled",
        "workflow_run",
        run.id,
        {"workflow_id": workflow_id, "approvals_cancelled": [str(i) for i in ended.cancelled]},
    )
    store.commit()
    _record_ended(ended)
    return get_workflow_run(workflow_id, run_id, org, db, store)
