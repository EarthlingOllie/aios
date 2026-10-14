"""Workflows: graphs of agents, and of other workflows run as subgraphs.

A workflow version is a directed acyclic graph. Each node pins an agent version or a workflow
version; an edge says control passes from one node to the next when it finishes. Nodes with no
incoming edge are where the workflow starts, and it ends when every node has run.

Running one (`execute_run`, in the worker) walks its pinned version's graph. A node starts once
all its predecessors have succeeded, so independent branches run at the same time. Its input is
their outputs, in edge order, joined with a blank line; start nodes get the run's input. An agent
node runs its agent version as a new agent run (one turn, `aios.runner`) and outputs the agent's
final reply. A workflow node runs its version's graph the same way, with the node's input, and
outputs its sink nodes' outputs (those with no outgoing edge), in node order. Each node records a
step (`workflow_run_steps`) when it starts and finishes, in its own transaction, so the run can be
watched as it goes. Once a node fails, no more nodes start; those already running finish, and the
run fails with the first error.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from anyio import CancelScope
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from aios import checkpoints, tracing
from aios.agent import ChatDeps
from aios.api.skills import lock_live
from aios.db import SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Workflow,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunStep,
    WorkflowStepStatus,
    WorkflowVersion,
)
from aios.runner import build_agent, run_title, version_tools
from aios.tools import AgentTool, ToolError
from aios.ui_stream import error_text

log = logging.getLogger(__name__)

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


# --- Running -------------------------------------------------------------------------------------


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
    # Exactly one of these, matching `kind`.
    agent: AgentNode | None = None
    graph: "Graph | None" = None


@dataclass(frozen=True)
class Graph:
    """A workflow version, loaded to run: its nodes in order, and the edges by node, in order."""

    nodes: list[Node]
    sources: dict[str, list[str]]
    targets: dict[str, list[str]]


def prefetch_graph(db: Session, version_id: int) -> dict[int, WorkflowVersion]:
    """Load a workflow version and every version it runs, at any depth, with what `load_graph`
    reads, in a fixed number of queries however many nodes, agents and levels it has.

    One recursive query finds the versions (pins only point at older versions, so the walk ends),
    then eager loads fetch their nodes, edges, agent versions, agents and tools. Rows already in
    the session are refreshed rather than skipped, so none of them lazy-loads per node later.
    (Safe here, unlike in `aios.api.workflows.load_version`: each row is reached by one path.)
    Keep the returned rows referenced while reading them: the session only holds rows weakly.
    """
    reach = (
        select(WorkflowVersion.id.label("id"))
        .where(WorkflowVersion.id == version_id)
        .cte("reach", recursive=True)
    )
    reach = reach.union(
        select(WorkflowNode.subworkflow_version_id)
        .join(reach, WorkflowNode.workflow_version_id == reach.c.id)
        .where(WorkflowNode.subworkflow_version_id.is_not(None))
    )
    agent_version = selectinload(WorkflowVersion.nodes).selectinload(WorkflowNode.agent_version)
    versions = db.scalars(
        select(WorkflowVersion)
        .where(WorkflowVersion.id.in_(select(reach.c.id)))
        .options(
            agent_version.selectinload(AgentVersion.agent),
            agent_version.selectinload(AgentVersion.tools),
            selectinload(WorkflowVersion.edges),
        )
        .execution_options(populate_existing=True)
    )
    return {version.id: version for version in versions}


def load_graph(db: Session, version_id: int) -> Graph:
    """A workflow version and every version it runs, at any depth, ready to run.

    Raises `RunError` if an agent version can't run: its agent has been deleted (an older version
    of a subworkflow can still pin it), or one of its tools can't be built from its stored
    settings (see `aios.tools.build_tool`). A version pinned by several nodes is loaded once.
    """
    return _build_graph(prefetch_graph(db, version_id), version_id, {})


def _build_graph(
    versions: dict[int, WorkflowVersion], version_id: int, loaded: dict[int, Graph]
) -> Graph:
    if version_id in loaded:
        return loaded[version_id]
    version = versions[version_id]
    nodes = []
    for node in version.nodes:
        if node.kind == WorkflowNodeKind.workflow:
            graph = _build_graph(versions, node.subworkflow_version_id, loaded)
            nodes.append(Node(node.key, node.kind, graph=graph))
            continue
        pinned = node.agent_version
        agent = pinned.agent
        if agent.deleted_at is not None:
            raise RunError(f"Agent {agent.name} v{pinned.version} can't run: it has been deleted")
        try:
            tools = version_tools(pinned)
        except ToolError as exc:
            raise RunError(f"Agent {agent.name} v{pinned.version} can't run: {exc}") from exc
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


def join_outputs(outputs: Sequence[str]) -> str:
    return "\n\n".join(outputs)


class StepFailed(Exception):
    """A node failed, and its step has been recorded as failed with `error`."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass
class RunState:
    """A run in progress."""

    id: int
    user_id: int
    workflow_id: int
    workflow_name: str
    workflow_version: int
    # The first node to fail, and why. Once set, no more nodes start.
    error: str | None = None
    failed_path: tuple[str, ...] = ()


def _claim(run_id: int) -> tuple[RunState, int, str] | None:
    """Mark a queued run running; return it, its version id and its input. None if it isn't
    queued (already started, e.g. a redelivered task, or deleted)."""
    with SessionLocal() as db:
        row = db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.queued)
            .values(status=WorkflowRunStatus.running, started_at=func.now())
            .returning(WorkflowRun.workflow_version_id, WorkflowRun.input)
        ).one_or_none()
        if row is None:
            return None
        version_id, text = row
        workflow, version = db.execute(
            select(Workflow, WorkflowVersion.version)
            .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
            .where(WorkflowVersion.id == version_id)
        ).one()
        db.commit()
    state = RunState(
        id=run_id,
        user_id=workflow.owner_id,
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        workflow_version=version,
    )
    return state, version_id, text


def _load(version_id: int) -> Graph:
    with SessionLocal() as db:
        return load_graph(db, version_id)


def _finish_run(
    run_id: int, status: WorkflowRunStatus, output: str | None, error: str | None
) -> None:
    with SessionLocal() as db:
        # A no-op if the run was deleted meanwhile, or has already finished.
        db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_([WorkflowRunStatus.queued, WorkflowRunStatus.running]),
            )
            .values(status=status, output=output, error=error, finished_at=func.now())
        )
        db.commit()


def fail_run(run_id: int, error: str) -> None:
    """Fail a run that couldn't finish, e.g. one stopped for taking too long."""
    _finish_run(run_id, WorkflowRunStatus.failed, None, error)


def _start_step(
    run_id: int, path: tuple[str, ...], node: Node, text: str
) -> tuple[int, int | None]:
    """Record that a node has started; return its step's id and, for an agent node, the agent
    run it's given (None if its agent has been deleted since the run started).

    The step and its agent run are committed together. Inserting the step share-locks the run's
    row, which deleting the run locks first, so a delete either waits and then sees this step's
    agent run, or wins and this fails, leaving no agent run behind.
    """
    with SessionLocal() as db:
        agent_run_id = None
        if node.agent is not None and lock_live(db, Agent, [node.agent.agent_id], read=True):
            agent_run = AgentRun(
                agent_id=node.agent.agent_id,
                agent_version_id=node.agent.agent_version_id,
                # Titled after its message, like a run after its first turn.
                title=run_title(text),
            )
            db.add(agent_run)
            db.flush()
            agent_run_id = agent_run.id
        step = WorkflowRunStep(
            run_id=run_id,
            path=list(path),
            kind=node.kind,
            status=WorkflowStepStatus.running,
            input=text,
            agent_run_id=agent_run_id,
        )
        db.add(step)
        db.commit()
        return step.id, agent_run_id


def _finish_step(
    step_id: int, status: WorkflowStepStatus, output: str | None, error: str | None
) -> None:
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRunStep)
            .where(WorkflowRunStep.id == step_id)
            .values(status=status, output=output, error=error, finished_at=func.now())
        )
        db.commit()


def trace_metadata(
    run: RunState, agent: AgentNode, agent_run_id: int, path: tuple[str, ...]
) -> dict[str, Any]:
    """Attached to an agent node's trace (Langfuse): an agent run's, plus the workflow's."""
    return tracing.trace_metadata(
        session_id=checkpoints.thread_id(agent_run_id),
        user_id=run.user_id,
        tags=[
            f"agent:{agent.agent_name}",
            f"agent-version:{agent.agent_version}",
            f"workflow:{run.workflow_name}",
        ],
        agent_run_id=agent_run_id,
        **agent.metadata,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow_name,
        workflow_version=run.workflow_version,
        workflow_run_id=run.id,
        workflow_path=list(path),
    )


async def _run_agent(
    run: RunState, agent: AgentNode, agent_run_id: int, path: tuple[str, ...], text: str
) -> str:
    """One turn of a new agent run, with `text` as the user's message; the reply's text."""
    # Imported here: the agents API imports the workflows API, which imports this module.
    from aios.api.agents import _record_turn

    config = {
        **checkpoints.thread_config(agent_run_id),
        "callbacks": tracing.callbacks(),
        "run_name": "agent-run",
        "metadata": trace_metadata(run, agent, agent_run_id, path),
    }
    async with checkpoints.turn(agent_run_id) as saver:
        graph = build_agent(
            model=agent.model,
            instructions=agent.instructions,
            tools=agent.tools,
            checkpointer=saver,
        )
        finished = False
        try:
            state = await graph.ainvoke(
                {"messages": [HumanMessage(text)]},
                config,
                context=ChatDeps(user_id=run.user_id, run_id=agent_run_id),
                # As in a chat turn: checkpointed step by step, so a failed turn's history is kept.
                durability="sync",
            )
            finished = True
        finally:
            # The message count for listings, and, if the turn failed, a thread the user can
            # carry on from on the run's page (see `_keep_unfinished_turn`).
            with CancelScope(shield=True):
                await _record_turn(graph, config, agent_run_id, finished=finished, partial=None)
    return state["messages"][-1].text


async def _run_node(run: RunState, node: Node, path: tuple[str, ...], text: str) -> str:
    """Run a node, recording its step; its output, or `StepFailed`."""
    step_id, agent_run_id = await asyncio.to_thread(_start_step, run.id, path, node, text)
    try:
        if node.graph is not None:
            output = await _run_graph(run, node.graph, path, text)
        elif agent_run_id is None:
            raise RunError(f"Agent {node.agent.agent_name} has been deleted")
        else:
            output = await _run_agent(run, node.agent, agent_run_id, path, text)
    except Exception as exc:
        if isinstance(exc, StepFailed):
            # A node of this subgraph failed; its step has the details.
            error = exc.error
        else:
            if not isinstance(exc, RunError):
                log.exception("workflow run %s: node %s failed", run.id, "/".join(path))
            error = error_text(exc)
            if run.error is None:
                run.error, run.failed_path = error, path
        await asyncio.to_thread(_finish_step, step_id, WorkflowStepStatus.failed, None, error)
        raise StepFailed(error) from exc
    await asyncio.to_thread(_finish_step, step_id, WorkflowStepStatus.succeeded, output, None)
    return output


async def _run_graph(run: RunState, graph: Graph, path: tuple[str, ...], text: str) -> str:
    """Run a graph's nodes, each once its predecessors have succeeded; its sinks' outputs.

    Raises the first of its nodes' failures, once the nodes already running have finished, or
    `StepFailed` if a failure elsewhere in the run stopped some of them from starting.
    """
    outputs: dict[str, str] = {}
    waiting = {key: len(sources) for key, sources in graph.sources.items()}
    by_key = {node.key: node for node in graph.nodes}
    running: dict[asyncio.Task[str], str] = {}
    failures: list[BaseException] = []

    def start(node: Node) -> None:
        sources = graph.sources[node.key]
        given = join_outputs([outputs[s] for s in sources]) if sources else text
        running[asyncio.create_task(_run_node(run, node, (*path, node.key), given))] = node.key

    for node in graph.nodes:
        if not waiting[node.key]:
            start(node)
    try:
        while running:
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                key = running.pop(task)
                if task.exception() is not None:
                    failures.append(task.exception())
                    continue
                outputs[key] = task.result()
                for target in graph.targets[key]:
                    waiting[target] -= 1
                    # Once any node of the run has failed, nothing more starts.
                    if not waiting[target] and run.error is None:
                        start(by_key[target])
    finally:
        # Only if this was cancelled itself: the nodes it started go with it.
        for task in running:
            task.cancel()
    if failures:
        raise failures[0]
    if len(outputs) < len(graph.nodes):
        # A node elsewhere in the run failed, so some of these never started.
        raise StepFailed("Stopped: another node failed")
    return join_outputs([outputs[n.key] for n in graph.nodes if not graph.targets[n.key]])


async def execute_run(run_id: int) -> None:
    """Run a queued workflow run to the end, recording its steps and then its result.

    Called by the worker (`aios.worker.run_workflow_task`). Does nothing unless the run is
    queued, so a task delivered twice runs it once.
    """
    claimed = await asyncio.to_thread(_claim, run_id)
    if claimed is None:
        return
    run, version_id, text = claimed
    try:
        # Loaded again rather than trusted from when the run was started: e.g. a tool may have
        # left the library since.
        graph = await asyncio.to_thread(_load, version_id)
        output = await _run_graph(run, graph, (), text)
    except StepFailed as exc:
        error = run.error or exc.error
        where = "/".join(run.failed_path)
        await asyncio.to_thread(fail_run, run_id, f"{where}: {error}" if where else error)
    except RunError as exc:
        await asyncio.to_thread(fail_run, run_id, str(exc))
    except Exception as exc:
        log.exception("workflow run %s failed", run_id)
        await asyncio.to_thread(fail_run, run_id, error_text(exc))
    else:
        await asyncio.to_thread(_finish_run, run_id, WorkflowRunStatus.succeeded, output, None)
