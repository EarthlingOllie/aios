"""Workflow runs on the run service, node by node.

A run holds no process while it goes. Two kinds of run service item move it on
(`aios.run_service.work`):

- **A workflow advance** (`advance_run`): a short coordinator step under the run's row lock. It
  rebuilds the run's state from its recorded steps, checks its time limit (active time only:
  time spent waiting on an approval doesn't count), records what has finished, starts the steps
  of the nodes now ready and enqueues their items, and ends the run once nothing is ready and
  nothing is running. Subworkflow nodes are expanded within it: a workflow node's step starts
  with its first nodes', and finishes, waits or fails with its graph. One advance per run is
  queued at a time (deduplicated by run); one enqueued while another runs makes that one run
  again once it's done (`Work.again_if_running`), so nothing a node recorded meanwhile is missed.
- **A workflow node** (`run_node`): an agent node's turn, on its own agent run, as a child
  invocation of the run's. Its step records how it went, and it enqueues an advance.

A node whose agent asks for a schema change waits for a member's decision: its step becomes
`waiting`, and once nothing else can run, so does the run. The decision enqueues an advance
(`aios.run_service.work.enqueue_resume`), which finds the waiting step's request decided and
starts the node again, resuming on its agent run's thread with the outcome; an expired request
reaches it the same way, as a rejection.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios import turns

# Read on the package at call time, where tests replace them.
from aios import workflows as package
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import AgentRun, Workflow, WorkflowRun, WorkflowRunStep, WorkflowVersion
from aios.models import WorkflowRunStatus, WorkflowStepStatus, WorkKind, WorkLane
from aios.run_service.queue import Work
from aios.ui_stream import error_text
from aios.workflows.execution import (
    NOT_ENDED,
    Ended,
    _end_runs,
    _load,
    decided_wait,
    fail_run,
    time_limit_error,
)
from aios.workflows.steps import (
    NodeWaiting,
    RunState,
    _run_agent,
    _start_step,
    _wait_step,
    join_outputs,
)
from aios.workflows.validation import Graph, Node, RunError, load_graph

log = logging.getLogger(__package__)

# Why a workflow node's step fails when another node's failure stopped its graph.
STOPPED_BY_FAILURE = "Stopped: another node failed"


def advance_key(data_store_id: uuid.UUID, run_id: int) -> str:
    return f"workflow-advance:{data_store_id}:{run_id}"


def advance_work(organisation_id: int, data_store_id: uuid.UUID, run_id: int) -> Work:
    """An advance of the store's run: one queued per run at a time."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.background,
        WorkKind.workflow_advance,
        {"run_id": run_id},
        dedup_key=advance_key(data_store_id, run_id),
        again_if_running=True,
    )


def node_work(
    organisation_id: int, data_store_id: uuid.UUID, run_id: int, step_id: int, resume: bool = False
) -> Work:
    """An agent node's turn, for its step: from the start, or resuming where it waited."""
    return Work(
        organisation_id,
        data_store_id,
        WorkLane.background,
        WorkKind.workflow_node,
        {"run_id": run_id, "step_id": step_id, "resume": resume},
        dedup_key=f"workflow-node:{data_store_id}:{step_id}",
    )


@dataclass(frozen=True)
class _Outcome:
    """How a node, or a graph, stands: succeeded (with its output), failed (with why), running
    or waiting."""

    status: WorkflowStepStatus
    output: str | None = None
    error: str | None = None


_RUNNING = _Outcome(WorkflowStepStatus.running)
_WAITING = _Outcome(WorkflowStepStatus.waiting)


@dataclass
class _Advance:
    """One advance of a run: its recorded steps by path, and the items it enqueues."""

    store: Session
    data_store_id: uuid.UUID
    organisation_id: int
    run: WorkflowRun
    steps: dict[tuple[str, ...], WorkflowRunStep]
    works: list[Work] = field(default_factory=list)
    # Once a node of the run has failed, no more nodes start.
    failed: bool = False

    def __post_init__(self) -> None:
        self.failed = any(s.status == WorkflowStepStatus.failed for s in self.steps.values())

    def graph(self, graph: Graph, path: tuple[str, ...], text: str) -> _Outcome:
        """Move a graph on: each node once its predecessors have succeeded, its input their
        outputs, in edge order, joined with a blank line (the graph's input for its first
        nodes); its output is its sinks' outputs, in node order."""
        outputs: dict[str, str] = {}
        settled: set[str] = set()
        running = waiting = False
        failures: list[_Outcome] = []
        changed = True
        while changed:
            changed = False
            for node in graph.nodes:
                sources = graph.sources[node.key]
                if node.key in settled or any(s not in outputs for s in sources):
                    continue
                given = join_outputs([outputs[s] for s in sources]) if sources else text
                outcome = self.node(node, (*path, node.key), given)
                if outcome is None:
                    continue
                settled.add(node.key)
                if outcome.status == WorkflowStepStatus.succeeded:
                    outputs[node.key] = outcome.output or ""
                    changed = True
                elif outcome.status == WorkflowStepStatus.failed:
                    failures.append(outcome)
                elif outcome.status == WorkflowStepStatus.running:
                    running = True
                else:
                    waiting = True
        if running:
            return _RUNNING
        if failures:
            return _Outcome(WorkflowStepStatus.failed, error=failures[0].error)
        if waiting:
            return _WAITING
        if len(outputs) < len(graph.nodes):
            # A node elsewhere in the run failed, so some of these never started.
            return _Outcome(WorkflowStepStatus.failed, error=STOPPED_BY_FAILURE)
        sinks = [outputs[n.key] for n in graph.nodes if not graph.targets[n.key]]
        return _Outcome(WorkflowStepStatus.succeeded, output=join_outputs(sinks))

    def node(self, node: Node, path: tuple[str, ...], given: str) -> _Outcome | None:
        """Move a ready node on, starting its step if it has none; None if it can't start."""
        step = self.steps.get(path)
        if step is None:
            if self.failed:
                return None
            step = _start_step(self.store, self.run.id, self.run.created_by, path, node, given)
            self.steps[path] = step
            if node.error is not None:
                return self._finish(step, _Outcome(WorkflowStepStatus.failed, error=node.error))
            if node.graph is None:
                self.works.append(self._node_work(step))
                return _RUNNING
        if step.status == WorkflowStepStatus.succeeded:
            return _Outcome(WorkflowStepStatus.succeeded, output=step.output or "")
        if step.status == WorkflowStepStatus.failed:
            return _Outcome(WorkflowStepStatus.failed, error=step.error)
        if node.graph is None:
            if step.status == WorkflowStepStatus.running:
                # Its item is queued or running already (one per step), unless it was lost.
                self.works.append(self._node_work(step))
                return _RUNNING
            if not self.failed and decided_wait(self.store, step):
                step.status, step.waiting_on_request_id = WorkflowStepStatus.running, None
                self.works.append(self._node_work(step, resume=True))
                return _RUNNING
            return _WAITING
        # A workflow node: its graph, from the input its step recorded.
        if node.error is not None:
            return self._finish(step, _Outcome(WorkflowStepStatus.failed, error=node.error))
        inner = self.graph(node.graph, path, step.input)
        if inner.status in (WorkflowStepStatus.succeeded, WorkflowStepStatus.failed):
            return self._finish(step, inner)
        step.status = inner.status
        return inner

    def _finish(self, step: WorkflowRunStep, outcome: _Outcome) -> _Outcome:
        step.status, step.output, step.error = outcome.status, outcome.output, outcome.error
        step.finished_at = func.now()
        self.store.flush()
        self.store.refresh(step, ["finished_at"])
        if outcome.status == WorkflowStepStatus.failed:
            self.failed = True
        return outcome

    def _node_work(self, step: WorkflowRunStep, resume: bool = False) -> Work:
        self.store.flush()
        return node_work(self.organisation_id, self.data_store_id, self.run.id, step.id, resume)


def _failure(steps: dict[tuple[str, ...], WorkflowRunStep], error: str | None) -> str:
    """Why the run failed: its first node's failure (one no node under it caused), and where."""
    failed = [s for s in steps.values() if s.status == WorkflowStepStatus.failed]
    if not failed:
        return error or "The run failed."
    first = [
        s for s in failed if not any(o is not s and o.path[: len(s.path)] == s.path for o in failed)
    ]
    step = min(first, key=lambda s: (s.finished_at is None, s.finished_at, s.id))
    return f"{'/'.join(step.path)}: {step.error}"


def _active_seconds(store: Session, run_id: int) -> float:
    """How long the run has been running, leaving out its time waiting."""
    ran = func.extract("epoch", func.now() - WorkflowRun.started_at) - WorkflowRun.waited_seconds
    return float(store.scalar(select(ran).where(WorkflowRun.id == run_id)) or 0)


def advance_run(
    data_store_id: uuid.UUID, organisation_id: int, run_id: int
) -> tuple[list[Work], Ended | None]:
    """Advance the store's run (see the module docstring): the items it enqueues, and the runs
    it ended (for `_record_ended` once committed). Nothing if the run is gone or has ended."""
    with store_session(data_store_id) as store:
        run = store.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
        if run is None or run.status not in NOT_ENDED:
            return [], None
        # Running from now: a waiting run adds the time it waited to `waited_seconds`.
        waited = func.coalesce(func.extract("epoch", func.now() - WorkflowRun.waiting_since), 0)
        store.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(
                status=WorkflowRunStatus.running,
                started_at=func.coalesce(WorkflowRun.started_at, func.now()),
                waited_seconds=WorkflowRun.waited_seconds + waited,
                waiting_since=None,
            )
            .execution_options(synchronize_session=False)
        )
        store.refresh(run)
        steps = {
            tuple(s.path): s
            for s in store.scalars(
                select(WorkflowRunStep)
                .where(WorkflowRunStep.run_id == run_id)
                .order_by(WorkflowRunStep.id)
            )
        }

        def end(status: WorkflowRunStatus, output: str | None, error: str | None) -> Ended:
            ended = _end_runs(store, [WorkflowRun.id == run_id], status, output, error)
            store.commit()
            return ended

        try:
            if package._organisation(data_store_id) is None:
                raise RunError("The run's organisation has been deleted.")
            if _active_seconds(store, run_id) > get_settings().workflow_run_time_limit:
                return [], end(WorkflowRunStatus.failed, None, time_limit_error())
            # Loaded again rather than trusted from when the run was started: e.g. a tool may
            # have left the library since. A run carrying on fails only at a node that runs
            # again (`Node.error`).
            graph = load_graph(store, run.workflow_version_id, organisation_id, strict=not steps)
            moving = _Advance(store, data_store_id, organisation_id, run, steps)
            outcome = moving.graph(graph, (), run.input)
        except RunError as exc:
            store.rollback()
            return [], end_after_rollback(store, run_id, str(exc))
        except Exception as exc:
            log.exception("workflow run %s: couldn't advance", run_id)
            store.rollback()
            return [], end_after_rollback(store, run_id, error_text(exc))
        if outcome.status == WorkflowStepStatus.succeeded:
            return [], end(WorkflowRunStatus.succeeded, outcome.output, None)
        if outcome.status == WorkflowStepStatus.failed:
            return [], end(WorkflowRunStatus.failed, None, _failure(steps, outcome.error))
        if outcome.status == WorkflowStepStatus.waiting:
            # Holding nothing until a decision enqueues its next advance.
            run.status, run.waiting_since = WorkflowRunStatus.waiting, func.now()
        store.commit()
        return moving.works, None


def end_after_rollback(store: Session, run_id: int, error: str) -> Ended:
    """Fail the run, after its advance was rolled back: a waiting one too, which nothing else
    would end."""
    store.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.waiting)
        .values(status=WorkflowRunStatus.running, waiting_since=None)
    )
    ended = _end_runs(store, [WorkflowRun.id == run_id], WorkflowRunStatus.failed, None, error)
    store.commit()
    return ended


def _time_up(data_store_id: uuid.UUID, run_id: int, agent_run_id: int | None) -> None:
    error = time_limit_error()
    if agent_run_id is not None:
        with store_session(data_store_id) as store:
            turns.fail_running(store, AgentRun.id == agent_run_id, error=error)
            store.commit()
    fail_run(data_store_id, run_id, error)


@dataclass(frozen=True)
class _NodeTurn:
    """What an agent node's item runs."""

    state: RunState
    node: Node
    path: tuple[str, ...]
    text: str
    agent_run_id: int | None
    time_left: float


def _find(graph: Graph, path: tuple[str, ...]) -> Node:
    for depth, key in enumerate(path):
        node = next(n for n in graph.nodes if n.key == key)
        if depth == len(path) - 1:
            return node
        if node.graph is None:
            raise RunError(f"No node at {'/'.join(path)}")
        graph = node.graph
    raise RunError("No node")


def _node_turn(data_store_id: uuid.UUID, run_id: int, step_id: int) -> _NodeTurn | None:
    """The node turn an item is for; None if its step isn't running (it was recorded, or the run
    ended), or its run isn't."""
    with store_session(data_store_id, "ro") as store:
        found = store.execute(
            select(WorkflowRunStep, WorkflowRun)
            .join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id)
            .where(WorkflowRunStep.id == step_id, WorkflowRun.id == run_id)
        ).one_or_none()
        if found is None:
            return None
        step, run = found
        if step.status != WorkflowStepStatus.running or run.status != WorkflowRunStatus.running:
            return None
        workflow_id, name, version = store.execute(
            select(Workflow.id, Workflow.name, WorkflowVersion.version)
            .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
            .where(WorkflowVersion.id == run.workflow_version_id)
        ).one()
        time_left = get_settings().workflow_run_time_limit - _active_seconds(store, run_id)
        path, text, agent_run_id = tuple(step.path), step.input, step.agent_run_id
        version_id, created_by = run.workflow_version_id, run.created_by
    organisation_id = package._organisation(data_store_id)
    if organisation_id is None:
        raise RunError("The run's organisation has been deleted.")
    graph = _load(data_store_id, organisation_id, version_id, strict=False)
    state = RunState(
        id=run_id,
        organisation_id=organisation_id,
        created_by=created_by,
        workflow_id=workflow_id,
        workflow_name=name,
        workflow_version=version,
        data_store_id=data_store_id,
    )
    return _NodeTurn(state, _find(graph, path), path, text, agent_run_id, time_left)


async def run_node(store_id: uuid.UUID, run_id: int, step_id: int, resume: bool) -> bool:
    """Run an agent node's turn (or carry it on where it waited, `resume`), and record how it
    went on its step. Whether the run should advance: False if there was nothing to do, or the
    run ended (its time ran out)."""
    try:
        turn = await asyncio.to_thread(_node_turn, store_id, run_id, step_id)
    except Exception as exc:
        if not isinstance(exc, RunError):
            log.exception("workflow run %s: step %s couldn't start", run_id, step_id)
        await asyncio.to_thread(
            package._finish_step,
            store_id,
            step_id,
            WorkflowStepStatus.failed,
            None,
            error_text(exc),
        )
        return True
    if turn is None:
        return False
    timeout = asyncio.timeout(max(turn.time_left, 0))
    try:
        if turn.node.error is not None:
            raise RunError(turn.node.error)
        if turn.agent_run_id is None:
            raise RunError(f"Agent {turn.node.agent.agent_name} has been deleted")
        async with timeout:
            output = await _run_agent(
                turn.state,
                turn.node.agent,
                turn.agent_run_id,
                turn.path,
                turn.text,
                resuming=resume,
            )
    except NodeWaiting as exc:
        await asyncio.to_thread(_wait_step, store_id, step_id, exc.request_id)
        return True
    except Exception as exc:
        if isinstance(exc, TimeoutError) and timeout.expired():
            # Its run's time is up: the run fails, with its steps still going, and the turn
            # that was cut off.
            await asyncio.to_thread(_time_up, store_id, run_id, turn.agent_run_id)
            return False
        if not isinstance(exc, RunError):
            log.exception("workflow run %s: node %s failed", run_id, "/".join(turn.path))
        await asyncio.to_thread(
            package._finish_step,
            store_id,
            step_id,
            WorkflowStepStatus.failed,
            None,
            error_text(exc),
        )
        return True
    await asyncio.to_thread(
        package._finish_step, store_id, step_id, WorkflowStepStatus.succeeded, output, None
    )
    return True
