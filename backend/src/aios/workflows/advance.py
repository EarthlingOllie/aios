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
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios import jev, turns

# Read on the package at call time, where tests replace them.
from aios import workflows as package
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import AgentRun, Workflow, WorkflowRun, WorkflowRunStep, WorkflowVersion
from aios.models import WorkflowRunStatus, WorkflowStepStatus, WorkKind, WorkLane
from aios.run_service.queue import Work
from aios.ui_stream import error_text
from aios.workflows import structured
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
    _record_step,
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
    """How a node, or a graph, stands: succeeded (with its output, as text and, if structured,
    as JSON), skipped, failed (with why), running or waiting."""

    status: WorkflowStepStatus
    output: str | None = None
    error: str | None = None
    output_json: Any = None

    @property
    def value(self) -> Any:
        """Its output as templates and conditions read it: the structured one, or the text."""
        return self.output_json if self.output_json is not None else (self.output or "")


_RUNNING = _Outcome(WorkflowStepStatus.running)
_WAITING = _Outcome(WorkflowStepStatus.waiting)
_SKIPPED = _Outcome(WorkflowStepStatus.skipped)
# Finished, so its successors may be ready.
_DONE = (WorkflowStepStatus.succeeded, WorkflowStepStatus.skipped)

# Where a predecessor's output passed on says it's from, in its untrusted block: derived text.
DERIVED = "derived:workflow:"


def derived(text: str, *keys: str) -> str:
    """Predecessors' output passed on to an agent node, wrapped as untrusted (`aios.jev.wrap`):
    derived text, never checked again but never trusted."""
    return jev.wrap(text, DERIVED + ",".join(keys))


@dataclass(frozen=True)
class _Given:
    """A node's, or a graph's, input: as text (what a person sees), as a value (what templates,
    conditions and schemas read: JSON, or the text) and as the message its agent's model is
    sent (with what's derived wrapped as untrusted)."""

    text: str
    value: Any
    message: str

    @property
    def derived(self) -> bool:
        return self.message != self.text

    @staticmethod
    def of_step(step: WorkflowRunStep) -> "_Given":
        value = step.input_json if step.input_json is not None else step.input
        return _Given(step.input, value, step.message or step.input)


class _InputFailed(Exception):
    """A node's input couldn't be built: a template's field is missing, or it doesn't fit the
    node's input schema. The node fails, naming why."""


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

    def graph(self, graph: Graph, path: tuple[str, ...], given: _Given) -> _Outcome:
        """Move a graph on: each node once every predecessor has finished (succeeded or been
        skipped), if any incoming edge is active (its source succeeded and its condition, if
        any, held), and otherwise skipped; start nodes always run. A node's input is its
        template's, or else its active predecessors' outputs, in edge order, joined with a blank
        line (the graph's input for its first nodes). Its output is its final nodes' that ran
        (`_output`)."""
        done: dict[str, _Outcome] = {}
        settled: set[str] = set()
        running = waiting = False
        failures: list[_Outcome] = []
        changed = True
        while changed:
            changed = False
            for node in graph.nodes:
                sources = graph.sources[node.key]
                if node.key in settled or any(s not in done for s in sources):
                    continue
                at = (*path, node.key)
                live = [s for s in sources if self._active(graph, path, s, node.key, done[s])]
                if sources and not live:
                    outcome = self.skip(node, at)
                else:
                    outcome = self.node(
                        node, at, lambda n=node, live=live: self._input(graph, n, live, done, given)
                    )
                if outcome is None:
                    continue
                settled.add(node.key)
                if outcome.status in _DONE:
                    if outcome.status == WorkflowStepStatus.succeeded:
                        self._conditions(graph, path, node.key, outcome, given)
                    done[node.key] = outcome
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
        if len(done) < len(graph.nodes):
            # A node elsewhere in the run failed, so some of these never started.
            return _Outcome(WorkflowStepStatus.failed, error=STOPPED_BY_FAILURE)
        return _output(graph, done)

    def _active(
        self, graph: Graph, path: tuple[str, ...], source: str, target: str, outcome: _Outcome
    ) -> bool:
        """Whether the edge is active: its source succeeded, and its condition, if it has one,
        held when evaluated (as recorded on the source's step, `_conditions`)."""
        if outcome.status != WorkflowStepStatus.succeeded:
            return False
        if (source, target) not in graph.conditions:
            return True
        recorded = self.steps[(*path, source)].conditions or {}
        return bool(recorded.get(target, False))

    def _conditions(
        self, graph: Graph, path: tuple[str, ...], key: str, outcome: _Outcome, given: _Given
    ) -> None:
        """Evaluate the conditions on a succeeded node's outgoing edges, once, and record them on
        its step: a resumed run follows what was recorded rather than evaluating them again.
        One that can't be evaluated fails the run, naming the edge (`RunError`)."""
        targets = [t for t in graph.targets[key] if (key, t) in graph.conditions]
        step = self.steps[(*path, key)]
        if not targets or step.conditions is not None:
            return
        data = {"output": outcome.value, "input": given.value}
        results = {}
        for target in targets:
            try:
                results[target] = structured.truthy(
                    structured.evaluate(graph.conditions[(key, target)], data)
                )
            except structured.ConditionError as exc:
                edge = f"{'/'.join((*path, key))} → {target}"
                raise RunError(f"The condition on {edge} couldn't be evaluated: {exc}") from exc
        step.conditions = results
        self.store.flush()

    def _input(
        self,
        graph: Graph,
        node: Node,
        live: list[str],
        done: dict[str, _Outcome],
        given: _Given,
    ) -> _Given:
        """A ready node's input (see `graph`). Raises `_InputFailed`."""
        if node.input_template is not None:
            try:
                rendered = structured.render(
                    node.input_template,
                    given.value,
                    {s: done[s].value for s in live},
                    derived,
                    derived_input=given.derived,
                )
            except structured.TemplateMissing as exc:
                raise _InputFailed(str(exc)) from exc
            result = _Given(rendered.text, rendered.value, rendered.message)
        elif not graph.sources[node.key]:
            result = given
        else:
            text = join_outputs([done[s].output or "" for s in live])
            value = done[live[0]].value if len(live) == 1 else text
            result = _Given(text, value, derived(text, *live))
        checks = [(node.input_schema, "node's")]
        if node.graph is not None:
            checks.append((node.graph.input_schema, "workflow's"))
        for schema, whose in checks:
            if schema is not None and (errors := structured.schema_errors(result.value, schema)):
                raise _InputFailed(
                    f"The input doesn't fit the {whose} input schema: {structured.describe(errors)}"
                )
        return result

    def skip(self, node: Node, path: tuple[str, ...]) -> _Outcome | None:
        """Record that no active path reached a ready node (no agent run); None if nothing can
        start any more."""
        step = self.steps.get(path)
        if step is not None:
            return _recorded(step) or _SKIPPED
        if self.failed:
            return None
        step = _record_step(self.store, self.run.id, path, node, WorkflowStepStatus.skipped)
        self.steps[path] = step
        return _SKIPPED

    def node(self, node: Node, path: tuple[str, ...], make_input) -> _Outcome | None:
        """Move a ready node on, starting its step (with its input, `make_input()`) if it has
        none; None if it can't start."""
        step = self.steps.get(path)
        if step is None:
            if self.failed:
                return None
            try:
                given = make_input()
            except _InputFailed as exc:
                failed = _Outcome(WorkflowStepStatus.failed, error=str(exc))
                step = _record_step(
                    self.store, self.run.id, path, node, failed.status, error=failed.error
                )
                self.steps[path] = step
                self.failed = True
                return failed
            step = _start_step(self.store, self.run.id, self.run.created_by, path, node, given.text)
            # Recorded beside it: its input as JSON, if structured, and what its model is sent.
            step.input_json = None if isinstance(given.value, str) else given.value
            step.message = given.message if given.message != given.text else None
            self.store.flush()
            self.steps[path] = step
            if node.error is not None:
                return self._finish(step, _Outcome(WorkflowStepStatus.failed, error=node.error))
            if node.graph is None:
                self.works.append(self._node_work(step))
                return _RUNNING
        if (recorded := _recorded(step)) is not None:
            return recorded
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
        inner = self.graph(node.graph, path, _Given.of_step(step))
        errors = (
            structured.schema_errors(inner.value, node.output_schema)
            if inner.status == WorkflowStepStatus.succeeded and node.output_schema is not None
            else []
        )
        if errors:
            why = structured.describe(errors)
            inner = _Outcome(
                WorkflowStepStatus.failed,
                error=f"The output doesn't fit the node's output schema: {why}",
            )
        if inner.status in (WorkflowStepStatus.succeeded, WorkflowStepStatus.failed):
            return self._finish(step, inner)
        step.status = inner.status
        return inner

    def _finish(self, step: WorkflowRunStep, outcome: _Outcome) -> _Outcome:
        step.status, step.output, step.error = outcome.status, outcome.output, outcome.error
        step.output_json = outcome.output_json
        step.finished_at = func.now()
        self.store.flush()
        self.store.refresh(step, ["finished_at"])
        if outcome.status == WorkflowStepStatus.failed:
            self.failed = True
        return outcome

    def _node_work(self, step: WorkflowRunStep, resume: bool = False) -> Work:
        self.store.flush()
        return node_work(self.organisation_id, self.data_store_id, self.run.id, step.id, resume)


def _recorded(step: WorkflowRunStep) -> _Outcome | None:
    """How a step that has ended went; None if it hasn't."""
    if step.status == WorkflowStepStatus.succeeded:
        return _Outcome(step.status, output=step.output or "", output_json=step.output_json)
    if step.status == WorkflowStepStatus.failed:
        return _Outcome(step.status, error=step.error)
    if step.status == WorkflowStepStatus.skipped:
        return _SKIPPED
    return None


def _output(graph: Graph, done: dict[str, _Outcome]) -> _Outcome:
    """A finished graph's output, from its final nodes (those with no outgoing edge) that ran:
    as text, their outputs joined in node order; as a value, one's output, or an object of
    several's by node. None ran: success with no output ("No final step ran"). Checked against
    the version's output schema, if it has one: a mismatch fails it."""
    ran = [
        n.key
        for n in graph.nodes
        if not graph.targets[n.key] and done[n.key].status == WorkflowStepStatus.succeeded
    ]
    if not ran:
        return _Outcome(WorkflowStepStatus.succeeded)
    text = join_outputs([done[key].output or "" for key in ran])
    value = done[ran[0]].value if len(ran) == 1 else {key: done[key].value for key in ran}
    schema = graph.output_schema
    if schema is not None and (errors := structured.schema_errors(value, schema)):
        return _Outcome(
            WorkflowStepStatus.failed,
            error="The output doesn't fit the workflow's output schema: "
            + structured.describe(errors),
        )
    return _Outcome(
        WorkflowStepStatus.succeeded,
        output=text,
        output_json=None if isinstance(value, str) else value,
    )


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

        def end(
            status: WorkflowRunStatus,
            output: str | None,
            error: str | None,
            output_json: Any = None,
        ) -> Ended:
            ended = _end_runs(
                store, [WorkflowRun.id == run_id], status, output, error, output_json=output_json
            )
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
            run_input = run.input_json if run.input_json is not None else run.input
            outcome = moving.graph(graph, (), _Given(run.input, run_input, run.input))
        except RunError as exc:
            store.rollback()
            return [], end_after_rollback(store, run_id, str(exc))
        except Exception as exc:
            log.exception("workflow run %s: couldn't advance", run_id)
            store.rollback()
            return [], end_after_rollback(store, run_id, error_text(exc))
        if outcome.status == WorkflowStepStatus.succeeded:
            return [], end(WorkflowRunStatus.succeeded, outcome.output, None, outcome.output_json)
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
        # What its model is sent: its input, with what's derived wrapped as untrusted.
        path, text, agent_run_id = tuple(step.path), step.message or step.input, step.agent_run_id
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
            output, output_json = await _run_agent(
                turn.state,
                turn.node.agent,
                turn.agent_run_id,
                turn.path,
                turn.text,
                resuming=resume,
                output_schema=turn.node.output_schema,
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
        package._finish_step,
        store_id,
        step_id,
        WorkflowStepStatus.succeeded,
        output,
        None,
        output_json,
    )
    return True
