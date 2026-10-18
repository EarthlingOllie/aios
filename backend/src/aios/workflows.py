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

An agent node whose agent asks for a schema change (`request_schema_change`, a LangGraph
interrupt) waits for the owner's decision: its step becomes `waiting`, with the request, and the
nodes that don't depend on it carry on. Once nothing else can run, the run becomes `waiting` and
its task returns (`_pause_run`), holding no worker. The decision queues it again
(`aios.worker.enqueue_resume`): claimed from `waiting`, it rebuilds its state from the steps it
recorded, and the waiting node resumes on its agent run's thread with the outcome. It's still on
the versions it pinned; a pin that can no longer run fails its step when it runs again. Waiting
time isn't running time: the run's time limit counts only the latter (`Claimed.time_left`).

While a worker runs a run, it refreshes the run's heartbeat (`heartbeat_at`) every
`HEARTBEAT_SECONDS`, from a thread of its own, so a node that holds the event loop doesn't stop
it (`_Heartbeat`). A run is alive while it's running with a recent heartbeat (`alive`); one
running without, whose worker died (killed, OOM, restarted), is failed when its task comes round
again or when it's read (`fail_stopped_runs`), and can be deleted. The same thread fails a run
whose worker's event loop has made no progress for `workflow_run_no_progress_limit` (stuck in a
blocking call, a deadlock), and stops it if the loop frees up; a node awaiting a slow call keeps
the loop free, so it isn't failed.
"""

import asyncio
import contextlib
import logging
import re
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from anyio import CancelScope
from langchain_core.messages import HumanMessage
from sqlalchemy import ColumnElement, and_, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from aios import checkpoints, invocations, tracing
from aios.agent import ChatDeps
from aios.api.skills import lock_live
from aios.config import get_settings
from aios.datastores import data_store_for, history, schema_changes
from aios.db import SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentVersion,
    SchemaChangeRequest,
    SchemaChangeStatus,
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

    Raises `RunError` if an agent version can't run: its agent has been deleted (an older version
    of a subworkflow can still pin it), or one of its tools can't be built from its stored
    settings (see `aios.tools.build_tool`). Not `strict` (a run resuming, whose nodes that have
    run needn't run again), such a node is loaded with the reason instead (`Node.error`), and
    fails if it runs. A version pinned by several nodes is loaded once.
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
            if agent.deleted_at is not None:
                raise RunError(
                    f"Agent {agent.name} v{pinned.version} can't run: it has been deleted"
                )
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


def join_outputs(outputs: Sequence[str]) -> str:
    return "\n\n".join(outputs)


class StepFailed(Exception):
    """A node failed, and its step (if it got one) has been recorded as failed with `error`."""

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
    # The data store its agents' tools use: the workflow owner's.
    data_store_id: uuid.UUID
    # The steps it recorded before it last paused, by path: a run resuming carries on from
    # them (see `_run_node`). Empty for a run starting.
    recorded: dict[tuple[str, ...], "RecordedStep"] = field(default_factory=dict)
    # The first node to fail, and why. Once set, no more nodes start.
    error: str | None = None
    failed_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordedStep:
    """A step a run recorded before it paused: succeeded, or waiting."""

    id: int
    status: WorkflowStepStatus
    input: str
    output: str | None
    agent_run_id: int | None


class NodeWaiting(Exception):
    """A node waits for a schema change request to be decided: an agent node whose agent asked
    for one, or a workflow node one of whose nodes waits. Its step is recorded waiting."""

    def __init__(self, request_id: uuid.UUID | None = None) -> None:
        super().__init__("waiting for a schema change request")
        # An agent node's: the request it waits on.
        self.request_id = request_id


# How often a worker refreshes the heartbeat of the run it's running, and how long without one
# until the run counts as stopped: a few missed beats, so a slow write doesn't fail a live run.
# (The beats are off the event loop, so a busy one doesn't delay them. One stuck for good is
# caught by the heartbeat too: see `_Heartbeat` and `Settings.workflow_run_no_progress_limit`.)
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# Why a run is failed once it's found running without a live worker.
WORKER_STOPPED = "The worker stopped while running this, so it couldn't finish."


def alive() -> ColumnElement[bool]:
    """Whether a run is still going: running, with a worker that has beaten lately. Never null,
    so it can be negated: a running run that isn't alive has lost its worker."""
    return and_(
        WorkflowRun.status == WorkflowRunStatus.running,
        func.coalesce(WorkflowRun.heartbeat_at > func.now() - STALE_AFTER, false()),
    )


def fail_stopped_runs(db: Session, *where: ColumnElement[bool]) -> None:
    """Fail the runs matching `where` that are running but not alive (see `alive`), and their
    steps that were cut off; commits."""
    _end_runs(
        db,
        [*where, WorkflowRun.status == WorkflowRunStatus.running, ~alive()],
        WorkflowRunStatus.failed,
        None,
        WORKER_STOPPED,
    )
    db.commit()


@dataclass(frozen=True)
class Claimed:
    """A run a worker has claimed to run, from queued or waiting."""

    state: RunState
    version_id: int
    input: str
    # What's left of its time limit: waiting doesn't count (seconds).
    time_left: float


def _claim(run_id: int) -> Claimed | None:
    """Mark a queued or waiting run running; return it. None if it's neither (deleted, ended,
    or already running), or if it's failed because it couldn't start.

    A waiting run is queued again once a request it waits on is decided (`enqueue_resume`);
    claimed, it adds the time it waited to `waited_seconds` and carries on from the steps it
    recorded (`RunState.recorded`).

    A run is queued once, and its task acked only when it ends, so a run found running was
    started by an earlier delivery. If that's still going (its heartbeat is fresh), this one
    leaves it be: the broker redelivers a message it's held longer than its visibility timeout
    (see `aios.worker`), which a run can outlast. If not, its worker died without finishing it
    (a hard time limit, OOM kill, restart); it can't go on from where it stopped, so it fails.
    """
    waited = func.extract("epoch", func.now() - WorkflowRun.waiting_since)
    ran = func.extract("epoch", func.now() - WorkflowRun.started_at) - WorkflowRun.waited_seconds
    with SessionLocal() as db:
        row = db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_([WorkflowRunStatus.queued, WorkflowRunStatus.waiting]),
            )
            .values(
                status=WorkflowRunStatus.running,
                started_at=func.coalesce(WorkflowRun.started_at, func.now()),
                heartbeat_at=func.now(),
                waited_seconds=WorkflowRun.waited_seconds + func.coalesce(waited, 0),
                waiting_since=None,
            )
            # After the update: `ran` is its running time so far.
            .returning(WorkflowRun.workflow_version_id, WorkflowRun.input, ran)
        ).one_or_none()
        if row is None:
            fail_stopped_runs(db, WorkflowRun.id == run_id)
            return None
        version_id, text, ran_seconds = row
        workflow, version = db.execute(
            select(Workflow, WorkflowVersion.version)
            .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
            .where(WorkflowVersion.id == version_id)
        ).one()
        recorded = {
            tuple(step.path): RecordedStep(
                id=step.id,
                status=step.status,
                input=step.input,
                output=step.output,
                agent_run_id=step.agent_run_id,
            )
            for step in db.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run_id))
        }
        # In the claim's transaction: if the store can't be resolved, the run isn't claimed...
        try:
            data_store_id = data_store_for(workflow.owner_id, db)
        except Exception as exc:
            db.rollback()
            error = exc
        else:
            error = None
            db.commit()
    if error is not None:
        # ...and it's failed now, with the real error: nothing else would ever run or end it. A
        # waiting run (being resumed) is failed too, rather than left waiting for good.
        log.exception("workflow run %s: couldn't resolve its data store", run_id, exc_info=error)
        with SessionLocal() as db:
            db.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.waiting)
                .values(status=WorkflowRunStatus.running, waiting_since=None)
            )
            db.commit()
        fail_run(run_id, error_text(error))
        return None
    state = RunState(
        id=run_id,
        user_id=workflow.owner_id,
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        workflow_version=version,
        data_store_id=data_store_id,
        recorded=recorded,
    )
    time_left = get_settings().workflow_run_time_limit - float(ran_seconds)
    return Claimed(state, version_id, text, time_left)


def _decided_waits() -> ColumnElement[bool]:
    """Whether a waiting step of the run (`WorkflowRun`) waits on a request that's been
    decided but not yet given to it."""
    request = SchemaChangeRequest
    return (
        select(WorkflowRunStep.id)
        .join(
            request,
            or_(
                request.id == WorkflowRunStep.waiting_on_request_id,
                request.requested_by_run == WorkflowRunStep.agent_run_id,
            ),
        )
        .where(
            WorkflowRunStep.run_id == WorkflowRun.id,
            WorkflowRunStep.status == WorkflowStepStatus.waiting,
            request.resumed_at.is_(None),
            request.status.not_in([SchemaChangeStatus.pending, SchemaChangeStatus.approved]),
        )
        .exists()
    )


def workflow_run_waiting_on(db: Session, request: SchemaChangeRequest) -> int | None:
    """The workflow run whose agent node made the request, if one did: the run to resume once
    it's decided. Found by the step's agent run, or, if that's gone with its agent, by the
    request the step waits on."""
    by_run = (
        WorkflowRunStep.agent_run_id == request.requested_by_run
        if request.requested_by_run is not None
        else false()
    )
    return db.scalar(
        select(WorkflowRunStep.run_id)
        .where(or_(by_run, WorkflowRunStep.waiting_on_request_id == request.id))
        .limit(1)
    )


def _pause_run(run_id: int) -> None:
    """A run whose nodes have all ended or wait: it waits, holding no worker.

    Queued again at once if a request it waits on was decided while it ran: that decision's
    own queuing found it running, and left it. (One decided later finds it waiting: this
    update holds the run's row until it commits, so that queuing's claim waits for it.)
    """
    with SessionLocal() as db:
        paused = db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.running)
            .values(status=WorkflowRunStatus.waiting, waiting_since=func.now())
        )
        decided = paused.rowcount > 0 and db.scalar(
            select(_decided_waits()).where(WorkflowRun.id == run_id)
        )
        db.commit()
    if decided:
        from aios.worker import run_workflow_task

        run_workflow_task.delay(run_id)


def _beat(run_id: int) -> bool:
    """Refresh the run's heartbeat; False if it isn't running any more (ended or deleted)."""
    with SessionLocal() as db:
        beat = db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.running)
            .values(heartbeat_at=func.now())
        )
        db.commit()
        return beat.rowcount > 0


class _Heartbeat:
    """Refreshes a run's heartbeat on a thread of its own until stopped, when the run ends.

    Not on the event loop, so a node that holds the loop (a long blocking call, CPU-bound work)
    doesn't let a live run go stale. Cancels `run`, on its loop, if the run has ended without it:
    failed as stopped because a beat was late (`fail_stopped_runs`), or deleted. Nobody will see
    the rest of its turns.

    It also watches the loop make progress: each beat hands the loop a callback that notes when
    it ran (`_progressed`), which it only does once the loop is free. A node awaiting a slow model
    or tool call leaves the loop free, so it keeps progressing however long it waits. If none has
    run for `workflow_run_no_progress_limit`, the loop is stuck (a blocking call that doesn't
    return, a deadlock) and so is the run: it fails the run with `no_progress_error`, stops
    beating, and cancels the run once the loop is free again. (A loop that never is keeps its
    worker until Celery's hard time limit, see `aios.worker`, but the run has ended.)
    """

    def __init__(self, run_id: int, run: asyncio.Task) -> None:
        self.run_id = run_id
        self.run = run
        self.loop = asyncio.get_running_loop()
        # When the loop last ran a callback this handed it (`time.monotonic`).
        self.progressed_at = time.monotonic()
        self.stopping = threading.Event()
        # Whether it stopped the run: set on the loop, so only once the run has been cancelled.
        self.stopped_run = False
        self.thread = threading.Thread(
            target=self._beat_until_stopped, name=f"workflow-run-{run_id}-heartbeat", daemon=True
        )
        self.thread.start()

    def _beat_until_stopped(self) -> None:
        limit = get_settings().workflow_run_no_progress_limit
        while not self.stopping.wait(HEARTBEAT_SECONDS):
            if time.monotonic() - self.progressed_at > limit:
                self._fail_stuck_run()
                return
            # If the loop has closed, the run is over (and this is being stopped).
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._progressed)
            try:
                alive = _beat(self.run_id)
            except Exception:
                # The run goes on; it only counts as stopped after several missed beats.
                log.exception("workflow run %s: couldn't record its heartbeat", self.run_id)
                continue
            if not alive:
                # Also once the run has just finished itself; then by the time this is called on
                # the loop it's done or being stopped, or it's cancelled after its result was
                # written, which changes nothing. If the loop has closed, the run is over.
                with contextlib.suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self._stop_run)
                return

    def _progressed(self) -> None:
        self.progressed_at = time.monotonic()

    def _fail_stuck_run(self) -> None:
        log.error(
            "workflow run %s: its event loop has made no progress for %ss; failing it",
            self.run_id,
            get_settings().workflow_run_no_progress_limit,
        )
        try:
            fail_run(self.run_id, no_progress_error())
        except Exception:
            # Without beats, it's failed as stopped once it's read (`fail_stopped_runs`).
            log.exception("workflow run %s: couldn't record that it's stuck", self.run_id)
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._stop_run)

    def _stop_run(self) -> None:
        if self.stopping.is_set() or self.run.done():
            return
        log.warning("workflow run %s ended elsewhere; stopping it", self.run_id)
        self.stopped_run = True
        self.run.cancel()

    async def stop(self) -> None:
        """Stop beating, and wait for a beat underway: none comes once the run has ended."""
        self.stopping.set()
        await asyncio.to_thread(self.thread.join)


def _load(version_id: int, strict: bool = True) -> Graph:
    with SessionLocal() as db:
        return load_graph(db, version_id, strict=strict)


# A run that hasn't ended: what a worker ends itself, and a run waiting for a decision.
NOT_ENDED = (WorkflowRunStatus.queued, WorkflowRunStatus.running, WorkflowRunStatus.waiting)


def _end_runs(
    db: Session,
    where: Sequence[ColumnElement[bool]],
    status: WorkflowRunStatus,
    output: str | None,
    error: str | None,
    *,
    waiting_too: bool = False,
) -> None:
    """End the runs matching `where` that haven't ended, and fail their steps still running.
    A waiting run is left be (it isn't the worker's to end) unless `waiting_too`."""
    going = [s for s in NOT_ENDED if waiting_too or s != WorkflowRunStatus.waiting]
    waited = func.coalesce(func.extract("epoch", func.now() - WorkflowRun.waiting_since), 0)
    ended = db.scalars(
        update(WorkflowRun)
        .where(*where, WorkflowRun.status.in_(going))
        .values(
            status=status,
            output=output,
            error=error,
            finished_at=func.now(),
            waited_seconds=WorkflowRun.waited_seconds + waited,
            waiting_since=None,
        )
        .returning(WorkflowRun.id)
    ).all()
    if not ended:
        return
    # Before the steps forget the requests they wait on.
    close_requests(db, ended)
    # Those a chat started post their results back to it.
    invocations.finish_workflow_runs(db, ended)
    # However it ended (finished, failed, stopped), it goes in the run history export's outbox,
    # with its own update: once its row is locked, as deleting it locks it first.
    history.enqueue_workflow_runs(db, WorkflowRun.id.in_(ended))
    # Steps whose nodes were stopped with the run, e.g. when it took too long or its worker died:
    # they never recorded their own end. And those waiting, when another node failed meanwhile.
    db.execute(
        update(WorkflowRunStep)
        .where(
            WorkflowRunStep.run_id.in_(ended),
            WorkflowRunStep.status.in_([WorkflowStepStatus.running, WorkflowStepStatus.waiting]),
        )
        .values(
            status=WorkflowStepStatus.failed,
            error="The run stopped before this step finished.",
            finished_at=func.now(),
            waiting_on_request_id=None,
        )
    )


def cancel_runs(db: Session, run_ids: Sequence[int], error: str) -> None:
    """Stop these runs, however far they've got: queued, running or waiting. Each fails with
    `error`, its steps still going fail, and its requests are closed out (`close_requests`), so
    nothing resumes it. A worker running one finds it ended at its next beat and stops it
    (`_Heartbeat`), and starts no step after (`_start_step` needs the run). In the caller's
    transaction."""
    _end_runs(
        db,
        [WorkflowRun.id.in_(run_ids)],
        WorkflowRunStatus.failed,
        None,
        error,
        waiting_too=True,
    )


def close_requests(db: Session, workflow_run_ids) -> None:
    """Close out the schema change requests these workflow runs' agent nodes made and haven't
    been given: the runs have ended or are being deleted, so nothing will give them their
    outcomes. Pending ones are cancelled, so they can't be approved, and their notifications
    cleared (`schema_changes.cancel`); the others count as given, so the conversations don't
    wait on them (one being applied still is). Found by the steps' agent runs, or, for one
    whose agent run went with its agent, by the request the step waits on."""
    steps = select(WorkflowRunStep).where(WorkflowRunStep.run_id.in_(workflow_run_ids)).subquery()
    not_given = [
        or_(
            SchemaChangeRequest.requested_by_run.in_(
                select(steps.c.agent_run_id).where(steps.c.agent_run_id.is_not(None))
            ),
            SchemaChangeRequest.id.in_(
                select(steps.c.waiting_on_request_id).where(
                    steps.c.waiting_on_request_id.is_not(None)
                )
            ),
        ),
        SchemaChangeRequest.resumed_at.is_(None),
    ]
    schema_changes.cancel(db, *not_given)
    db.execute(update(SchemaChangeRequest).where(*not_given).values(resumed_at=func.now()))


def _finish_run(
    run_id: int, status: WorkflowRunStatus, output: str | None, error: str | None
) -> None:
    with SessionLocal() as db:
        # A no-op if the run was deleted meanwhile, or has already finished.
        _end_runs(db, [WorkflowRun.id == run_id], status, output, error)
        db.commit()


def time_limit_error() -> str:
    minutes = get_settings().workflow_run_time_limit // 60
    return f"The run took longer than {minutes} minutes, so it was stopped."


def no_progress_error() -> str:
    limit = get_settings().workflow_run_no_progress_limit
    took = f"{limit // 60:g} minutes" if limit >= 120 else f"{limit:g} seconds"
    return f"The run made no progress for {took}: something held up its worker, so it was stopped."


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
    agent run, or wins and this fails (a `RunError`), leaving no agent run behind. Nor does a
    node start once the run has ended without it (cancelled, say: `cancel_runs`): the run's row
    is share-locked, so an end being committed is waited for and then seen. The agent is locked
    first, as deleting it locks the agent and then ends its runs, so the two queue rather than
    deadlock.
    """
    with SessionLocal() as db:
        live = node.agent is not None and lock_live(db, Agent, [node.agent.agent_id], read=True)
        # Nor once the run has ended without it (`_end_runs`: e.g. failed as stopped while its
        # worker went on), whose steps, and their exported copy, are final. Share-locked, so
        # ending it waits for this, or this for that.
        status = db.scalar(
            select(WorkflowRun.status).where(WorkflowRun.id == run_id).with_for_update(read=True)
        )
        if status is None:
            raise RunError("The run has been deleted")
        if status != WorkflowRunStatus.running:
            raise RunError("The run has ended")
        agent_run_id = None
        if live:
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
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.get(WorkflowRun, run_id) is None:
                raise RunError("The run has been deleted") from None
            raise
        return step.id, agent_run_id


def _finish_step(
    step_id: int, status: WorkflowStepStatus, output: str | None, error: str | None
) -> None:
    with SessionLocal() as db:
        # Not once the run has ended without it (`_end_runs`), e.g. its node finished after the
        # run was failed for making no progress: the step stays failed with the run.
        db.execute(
            update(WorkflowRunStep)
            .where(
                WorkflowRunStep.id == step_id, WorkflowRunStep.status == WorkflowStepStatus.running
            )
            .values(status=status, output=output, error=error, finished_at=func.now())
        )
        db.commit()


def _wait_step(step_id: int, request_id: uuid.UUID | None) -> None:
    """Record that a running step waits (on `request_id`, for an agent node)."""
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRunStep)
            .where(
                WorkflowRunStep.id == step_id, WorkflowRunStep.status == WorkflowStepStatus.running
            )
            .values(status=WorkflowStepStatus.waiting, waiting_on_request_id=request_id)
        )
        db.commit()


def _resume_step(step_id: int) -> None:
    """Record that a waiting step is running again."""
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRunStep)
            .where(
                WorkflowRunStep.id == step_id, WorkflowRunStep.status == WorkflowStepStatus.waiting
            )
            .values(status=WorkflowStepStatus.running, waiting_on_request_id=None)
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
    run: RunState,
    agent: AgentNode,
    agent_run_id: int,
    path: tuple[str, ...],
    text: str,
    *,
    resuming: bool = False,
) -> str:
    """One turn of the node's agent run, with `text` as the user's message; the reply's text.

    Raises `NodeWaiting` if the agent asked for a schema change and waits for it to be decided
    (`request_schema_change`). `resuming`, the turn carries on from where it waited instead, on
    the same thread: each waiting tool call whose request is decided gets its outcome, as a chat
    resumes (`aios.api.agents.resume_run`), and the agent goes on from there.
    """
    # Imported here: the agents API imports this module.
    from aios.api.agents import Resume, _mark_resumed, _record_turn, _resume_command

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
        resume = Resume()
        if resuming:
            # None if none of its requests has been decided: it goes on waiting.
            graph_input = await _resume_command(graph, config, agent_run_id, resume)
        else:
            graph_input = {"messages": [HumanMessage(text)]}
        if graph_input is None and resume.dealt_with:
            # Decided, but no call waits on them any more (given just before a worker died,
            # say): they count as given, rather than having the run queued again for them.
            await asyncio.to_thread(_mark_resumed, agent_run_id, resume.dealt_with)
        if graph_input is not None:
            finished = False
            try:
                await graph.ainvoke(
                    graph_input,
                    config,
                    context=ChatDeps(
                        user_id=run.user_id,
                        run_id=agent_run_id,
                        data_store_id=run.data_store_id,
                    ),
                    # As in a chat turn: checkpointed step by step, so a failed turn's history is
                    # kept.
                    durability="sync",
                )
                finished = True
            finally:
                with CancelScope(shield=True):
                    if resume.dealt_with:
                        # Given to the run, which takes messages again unless another waits.
                        await asyncio.to_thread(_mark_resumed, agent_run_id, resume.dealt_with)
                    # The message count for listings, and, if the turn failed, a thread the user
                    # can carry on from on the run's page (see `_keep_unfinished_turn`).
                    await _record_turn(graph, config, agent_run_id, finished=finished, partial=None)
        state = await graph.aget_state(config)
    request_id = _waits_on(state)
    if request_id is not None:
        raise NodeWaiting(request_id)
    return state.values["messages"][-1].text


def _waits_on(state) -> uuid.UUID | None:
    """The schema change request an agent run's state waits on, if it waits on one (the first,
    if on several)."""
    from aios.tools.schema_change import REQUEST_KEY

    for waiting in state.interrupts:
        if isinstance(waiting.value, dict) and REQUEST_KEY in waiting.value:
            return uuid.UUID(waiting.value[REQUEST_KEY])
    return None


async def _run_node(run: RunState, node: Node, path: tuple[str, ...], text: str) -> str:
    """Run a node, recording its step; its output, `NodeWaiting` or `StepFailed`.

    For a run resuming, a node whose step succeeded before it paused gives that step's output
    again, and one whose step waited carries on: an agent node on its agent run's thread, a
    workflow node through its own graph. Either fails if its pinned agent version can't run any
    more (`Node.error`).
    """
    recorded = run.recorded.get(path)
    if recorded is not None and recorded.status == WorkflowStepStatus.succeeded:
        return recorded.output or ""
    step_id = None
    try:
        if recorded is None:
            step_id, agent_run_id = await asyncio.to_thread(_start_step, run.id, path, node, text)
        else:
            step_id, agent_run_id, text = recorded.id, recorded.agent_run_id, recorded.input
            await asyncio.to_thread(_resume_step, step_id)
        if node.error is not None:
            raise RunError(node.error)
        if node.graph is not None:
            output = await _run_graph(run, node.graph, path, text)
        elif agent_run_id is None:
            raise RunError(f"Agent {node.agent.agent_name} has been deleted")
        else:
            output = await _run_agent(
                run, node.agent, agent_run_id, path, text, resuming=recorded is not None
            )
    except NodeWaiting as exc:
        await asyncio.to_thread(_wait_step, step_id, exc.request_id)
        raise NodeWaiting from None
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
        # None if the step couldn't be recorded (e.g. the run has been deleted).
        if step_id is not None:
            await asyncio.to_thread(_finish_step, step_id, WorkflowStepStatus.failed, None, error)
        raise StepFailed(error) from exc
    await asyncio.to_thread(_finish_step, step_id, WorkflowStepStatus.succeeded, output, None)
    return output


async def _run_graph(run: RunState, graph: Graph, path: tuple[str, ...], text: str) -> str:
    """Run a graph's nodes, each once its predecessors have succeeded; its sinks' outputs.

    Raises the first of its nodes' failures, once the nodes already running have finished, or
    `StepFailed` if a failure elsewhere in the run stopped some of them from starting. Without a
    failure, raises `NodeWaiting` if any of its nodes waits, once nothing else of it can run:
    nodes that don't depend on a waiting one still start.
    """
    outputs: dict[str, str] = {}
    waiting = {key: len(sources) for key, sources in graph.sources.items()}
    by_key = {node.key: node for node in graph.nodes}
    running: dict[asyncio.Task[str], str] = {}
    failures: list[BaseException] = []
    paused = False

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
                if isinstance(task.exception(), NodeWaiting):
                    paused = True
                    continue
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
    if paused:
        raise NodeWaiting
    if len(outputs) < len(graph.nodes):
        # A node elsewhere in the run failed, so some of these never started.
        raise StepFailed("Stopped: another node failed")
    return join_outputs([outputs[n.key] for n in graph.nodes if not graph.targets[n.key]])


async def execute_run(run_id: int) -> None:
    """Run a queued workflow run, or carry on a waiting one, to the end or until it waits,
    recording its steps and then its result.

    Called by the worker (`aios.worker.run_workflow_task`). Does nothing unless the run is
    queued or waiting, so a task delivered twice runs it once; a run left running by a worker
    that died is failed instead (see `_claim`). Its heartbeat is refreshed until it ends or
    waits, and stops it if it has ended elsewhere meanwhile.
    """
    claimed = await asyncio.to_thread(_claim, run_id)
    if claimed is None:
        return
    run = asyncio.create_task(_execute(claimed))
    heartbeat = _Heartbeat(run_id, run)
    try:
        await run
    except asyncio.CancelledError:
        # Stopped by its heartbeat; propagate any other cancellation (e.g. of this coroutine).
        if not heartbeat.stopped_run:
            raise
    finally:
        run.cancel()
        await heartbeat.stop()


def _failure(run: RunState, error: str) -> str:
    """Why the run failed: its first node's failure, and where."""
    error = run.error or error
    where = "/".join(run.failed_path)
    return f"{where}: {error}" if where else error


async def _execute(claimed: Claimed) -> None:
    run = claimed.state
    try:
        # Timed here rather than by Celery's soft limit, whose exception can land inside a node
        # and fail it like any other error. Only running counts: a run resuming has what's left.
        async with asyncio.timeout(claimed.time_left):
            # Loaded again rather than trusted from when the run was started: e.g. a tool may
            # have left the library since. A run resuming fails only at a node that runs again.
            graph = await asyncio.to_thread(_load, claimed.version_id, not run.recorded)
            output = await _run_graph(run, graph, (), claimed.input)
    except TimeoutError:
        await asyncio.to_thread(fail_run, run.id, time_limit_error())
    except NodeWaiting:
        if run.error is None:
            await asyncio.to_thread(_pause_run, run.id)
        else:
            # A node failed while another waits: the run fails, as it would have had the other
            # finished.
            await asyncio.to_thread(fail_run, run.id, _failure(run, run.error))
    except StepFailed as exc:
        await asyncio.to_thread(fail_run, run.id, _failure(run, exc.error))
    except RunError as exc:
        await asyncio.to_thread(fail_run, run.id, str(exc))
    except Exception as exc:
        log.exception("workflow run %s failed", run.id)
        await asyncio.to_thread(fail_run, run.id, error_text(exc))
    else:
        await asyncio.to_thread(_finish_run, run.id, WorkflowRunStatus.succeeded, output, None)
