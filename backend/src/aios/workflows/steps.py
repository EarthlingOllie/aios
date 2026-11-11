"""Running a run's graph, node by node: each node's step, recorded as it starts, finishes, waits
and resumes, and an agent node's turn."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from anyio import CancelScope
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from aios import checkpoints, tracing

# Read on the package at call time, where tests replace them.
from aios import workflows as package
from aios.agent import ChatDeps
from aios.db import SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunStep,
    WorkflowStepStatus,
)
from aios.runner import build_agent, run_title
from aios.ui_stream import error_text
from aios.workflows.validation import AgentNode, Graph, Node, RunError

log = logging.getLogger(__package__)


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
    # It executes for the workflow's organisation, over the organisation's store, whoever
    # started it: the member who did (None for a schedule's run), which its agent nodes' runs
    # record as theirs, and their tools and traces are told.
    organisation_id: int
    created_by: int | None
    workflow_id: int
    workflow_name: str
    workflow_version: int
    # The data store its agents' tools use: the organisation's.
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


def _start_step(
    run_id: int, created_by: int | None, path: tuple[str, ...], node: Node, text: str
) -> tuple[int, int | None]:
    """Record that a node has started; return its step's id and, for an agent node, the agent
    run it's given, started by whoever started the workflow run (`created_by`). Its agent may
    have been archived since the run started: a run already going still runs it.

    The step and its agent run are committed together. Inserting the step share-locks the run's
    row, which deleting the run locks first, so a delete either waits and then sees this step's
    agent run, or wins and this fails (a `RunError`), leaving no agent run behind. Nor does a
    node start once the run has ended without it: the run's row is share-locked, so an end being
    committed is waited for and then seen.
    """
    with SessionLocal() as db:
        live = node.agent is not None
        if live:
            db.execute(
                select(Agent.id).where(Agent.id == node.agent.agent_id).with_for_update(read=True)
            )
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
                created_by=created_by,
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
        organisation_id=run.organisation_id,
        created_by=run.created_by,
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
        "metadata": package.trace_metadata(run, agent, agent_run_id, path),
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
                        user_id=run.created_by,
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
            step_id, agent_run_id = await asyncio.to_thread(
                package._start_step, run.id, run.created_by, path, node, text
            )
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
            await asyncio.to_thread(
                package._finish_step, step_id, WorkflowStepStatus.failed, None, error
            )
        raise StepFailed(error) from exc
    await asyncio.to_thread(
        package._finish_step, step_id, WorkflowStepStatus.succeeded, output, None
    )
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
