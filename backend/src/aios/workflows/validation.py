"""A workflow's graph: checked before it's saved (`validate_graph`), checked before a run of it
starts that nothing in it is archived (`archived_in_graph`), and loaded to run, with the agent
versions it pins checked that they still can (`load_graph`)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aios import tracing
from aios.models import (
    Agent,
    AgentVersion,
    Workflow,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowVersion,
)
from aios.runner import version_tools
from aios.tools import AgentTool, ToolError

KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class GraphError(ValueError):
    """A workflow graph that can't be saved; the message is shown to the user."""


def validate_graph(keys: Sequence[str], edges: Sequence[tuple[str, str]]) -> None:
    """Checks node keys are valid and unique, and edges join existing nodes without a cycle."""
    for key in keys:
        if not KEY_PATTERN.fullmatch(key):
            raise GraphError(f"Invalid node key {key!r}: use 1-64 letters, digits, '-' or '_'.")
    if len(set(keys)) != len(keys):
        raise GraphError("Each node key can be used only once")
    known = set(keys)
    if len(set(edges)) != len(edges):
        raise GraphError("Each edge can be added only once")
    successors: dict[str, list[str]] = {key: [] for key in keys}
    for source, target in edges:
        for end in (source, target):
            if end not in known:
                raise GraphError(f"An edge refers to an unknown node: {end!r}")
        if source == target:
            raise GraphError(f"Node {source!r} can't lead to itself")
        successors[source].append(target)
    # Kahn's algorithm: whatever never runs out of incoming edges is on or downstream of a cycle.
    incoming = {key: 0 for key in keys}
    for _, target in edges:
        incoming[target] += 1
    ready = [key for key in keys if incoming[key] == 0]
    seen = 0
    while ready:
        key = ready.pop()
        seen += 1
        for target in successors[key]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if seen != len(keys):
        # Each leftover node still has a leftover predecessor, so walking back through
        # them from any one must come round to a node already visited: that's a cycle.
        back = {target: source for source, target in edges if incoming[source] and incoming[target]}
        path = [next(key for key in keys if incoming[key])]
        while path[-1] not in path[:-1]:
            path.append(back[path[-1]])
        cycle = path[path.index(path[-1]) :][::-1]
        raise GraphError(f"The graph has a cycle: {' → '.join(cycle)}")


# --- Loading to run -------------------------------------------------------------------------------


class RunError(Exception):
    """A workflow version that can't run, or a node that failed; the message is shown to the
    user."""


@dataclass(frozen=True)
class AgentNode:
    """What an agent node runs: its pinned version, with the tools built."""

    agent_id: int
    agent_name: str
    agent_version_id: int
    agent_version: int
    model: str | None
    instructions: str
    tools: list[AgentTool]
    # The version's part of its runs' trace metadata (see `tracing.agent_version_metadata`).
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Node:
    key: str
    kind: WorkflowNodeKind
    # Exactly one of these, matching `kind`; neither for an agent node that can't run.
    agent: AgentNode | None = None
    graph: "Graph | None" = None
    # Why its pinned agent version can't run any more (see `load_graph`).
    error: str | None = None


@dataclass(frozen=True)
class Graph:
    """A workflow version, loaded to run: its nodes in order, and the edges by node, in order."""

    nodes: list[Node]
    sources: dict[str, list[str]]
    targets: dict[str, list[str]]


def reachable_versions(version_id: int):
    """A CTE of the ids (`id`) of a workflow version and every version it runs, at any depth: one
    recursive query (pins only point at older versions, so the walk ends)."""
    reach = (
        select(WorkflowVersion.id.label("id"))
        .where(WorkflowVersion.id == version_id)
        .cte("reach", recursive=True)
    )
    return reach.union(
        select(WorkflowNode.subworkflow_version_id)
        .join(reach, WorkflowNode.workflow_version_id == reach.c.id)
        .where(WorkflowNode.subworkflow_version_id.is_not(None))
    )


def archived_in_graph(db: Session, version_id: int) -> str | None:
    """Why a run of this workflow version can't start because of what it runs, at any depth: an
    agent or workflow its graph pins is archived ("The agent <name> is archived."), the first
    found by name; None if nothing is. Runs already going aren't checked again, so their nodes
    still run."""
    reach = reachable_versions(version_id)
    nodes = (
        select(WorkflowNode)
        .where(WorkflowNode.workflow_version_id.in_(select(reach.c.id)))
        .subquery()
    )
    agent = db.scalar(
        select(Agent.name)
        .join(AgentVersion, AgentVersion.agent_id == Agent.id)
        .join(nodes, nodes.c.agent_version_id == AgentVersion.id)
        .where(Agent.archived_at.is_not(None))
        .order_by(Agent.name)
        .limit(1)
    )
    if agent is not None:
        return f"The agent {agent} is archived."
    workflow = db.scalar(
        select(Workflow.name)
        .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
        .join(nodes, nodes.c.subworkflow_version_id == WorkflowVersion.id)
        .where(Workflow.archived_at.is_not(None))
        .order_by(Workflow.name)
        .limit(1)
    )
    if workflow is not None:
        return f"The workflow {workflow} is archived."
    return None


def prefetch_graph(db: Session, version_id: int) -> dict[int, WorkflowVersion]:
    """Load a workflow version and every version it runs, at any depth, with what `load_graph`
    reads, in a fixed number of queries however many nodes, agents and levels it has.

    One recursive query finds the versions (pins only point at older versions, so the walk ends),
    then eager loads fetch their nodes, edges, agent versions, agents and tools. Rows already in
    the session are refreshed rather than skipped, so none of them lazy-loads per node later.
    (Safe here, unlike in `aios.api.workflows.load_version`: each row is reached by one path.)
    Keep the returned rows referenced while reading them: the session only holds rows weakly.
    """
    reach = reachable_versions(version_id)
    agent_version = selectinload(WorkflowVersion.nodes).selectinload(WorkflowNode.agent_version)
    versions = db.scalars(
        select(WorkflowVersion)
        .where(WorkflowVersion.id.in_(select(reach.c.id)))
        .options(
            agent_version.selectinload(AgentVersion.agent),
            agent_version.selectinload(AgentVersion.tools),
            # For each agent node's trace metadata (`tracing.agent_version_metadata`).
            agent_version.selectinload(AgentVersion.skills),
            selectinload(WorkflowVersion.edges),
        )
        .execution_options(populate_existing=True)
    )
    return {version.id: version for version in versions}


def load_graph(db: Session, version_id: int, *, strict: bool = True) -> Graph:
    """A workflow version and every version it runs, at any depth, ready to run.

    Raises `RunError` if an agent version can't run: one of its tools can't be built from its
    stored settings (see `aios.tools.build_tool`). An archived agent's still can: what stops a
    run starting on one is `archived_in_graph`, and one already going runs its node. Not
    `strict` (a run resuming, whose nodes that have run needn't run again), such a node is
    loaded with the reason instead (`Node.error`), and fails if it runs. A version pinned by
    several nodes is loaded once.
    """
    return _build_graph(prefetch_graph(db, version_id), version_id, {}, strict)


def _build_graph(
    versions: dict[int, WorkflowVersion], version_id: int, loaded: dict[int, Graph], strict: bool
) -> Graph:
    if version_id in loaded:
        return loaded[version_id]
    version = versions[version_id]
    nodes = []
    for node in version.nodes:
        if node.kind == WorkflowNodeKind.workflow:
            graph = _build_graph(versions, node.subworkflow_version_id, loaded, strict)
            nodes.append(Node(node.key, node.kind, graph=graph))
            continue
        pinned = node.agent_version
        agent = pinned.agent
        try:
            try:
                tools = version_tools(pinned)
            except ToolError as exc:
                raise RunError(f"Agent {agent.name} v{pinned.version} can't run: {exc}") from exc
        except RunError as exc:
            if strict:
                raise
            nodes.append(Node(node.key, node.kind, error=str(exc)))
            continue
        agent_node = AgentNode(
            agent_id=agent.id,
            agent_name=agent.name,
            agent_version_id=pinned.id,
            agent_version=pinned.version,
            model=pinned.model,
            instructions=pinned.instructions,
            tools=tools,
            metadata=tracing.agent_version_metadata(pinned),
        )
        nodes.append(Node(node.key, node.kind, agent=agent_node))
    sources: dict[str, list[str]] = {node.key: [] for node in nodes}
    targets: dict[str, list[str]] = {node.key: [] for node in nodes}
    for edge in version.edges:
        sources[edge.target].append(edge.source)
        targets[edge.source].append(edge.target)
    loaded[version_id] = Graph(nodes, sources, targets)
    return loaded[version_id]
