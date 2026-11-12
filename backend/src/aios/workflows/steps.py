"""A run's steps, recorded as they start, finish and wait, and an agent node's turn: what the
run service's workflow node item runs (`aios.workflows.advance`)."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from anyio import CancelScope
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from aios import checkpoints, invocations, tracing, usage

# Read on the package at call time, where tests replace them.
from aios import workflows as package
from aios.agent import ChatDeps
from aios.datastores import store_session
from aios.datastores.models import AgentRun, Turn, WorkflowRunStep
from aios.models import (
    AgentRunStatus,
    InvocationStatus,
    TriggerTargetKind,
    WorkflowStepStatus,
)
from aios.runner import build_agent, run_title
from aios.ui_stream import error_text
from aios.workflows.validation import AgentNode, Node, RunError

log = logging.getLogger(__package__)


def join_outputs(outputs: Sequence[str]) -> str:
    return "\n\n".join(outputs)


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


class NodeWaiting(Exception):
    """A node waits for a schema change request to be decided: an agent node whose agent asked
    for one, or a workflow node one of whose nodes waits. Its step is recorded waiting."""

    def __init__(self, request_id: uuid.UUID | None = None) -> None:
        super().__init__("waiting for a schema change request")
        # An agent node's: the request it waits on.
        self.request_id = request_id


def _start_step(
    store: Session,
    run_id: int,
    created_by: int | None,
    path: tuple[str, ...],
    node: Node,
    text: str,
) -> WorkflowRunStep:
    """Record, in `store`'s transaction (an advance's, which holds the run's row lock), that a
    node of the store's running run has started; return its step. An agent node is given an
    agent run (in the same store), started by whoever started the workflow run (`created_by`),
    with its invocation, a child of the workflow run's. Its agent may have been archived since
    the run started: a run already going still runs it."""
    agent_run_id = None
    if node.agent is not None:
        agent_run = AgentRun(
            agent_id=node.agent.agent_id,
            agent_version_id=node.agent.agent_version_id,
            # Titled after its message, like a run after its first turn.
            title=run_title(text),
            created_by=created_by,
        )
        store.add(agent_run)
        store.flush()
        agent_run_id = agent_run.id
        # Its invocation: a child of the workflow run's, at any depth of subworkflow (they're
        # part of the same run).
        parent = invocations.of_workflow_run(store, run_id)
        invocation = invocations.for_run(
            store,
            TriggerTargetKind.agent,
            node.agent.agent_id,
            requested_by=created_by,
            parent=parent,
            status=InvocationStatus.running,
        )
        invocation.agent_run_id = agent_run_id
    step = WorkflowRunStep(
        run_id=run_id,
        path=list(path),
        kind=node.kind,
        status=WorkflowStepStatus.running,
        input=text,
        agent_run_id=agent_run_id,
    )
    store.add(step)
    store.flush()
    return step


def _finish_step(
    data_store_id: uuid.UUID,
    step_id: int,
    status: WorkflowStepStatus,
    output: str | None,
    error: str | None,
) -> None:
    with store_session(data_store_id) as db:
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


def _wait_step(data_store_id: uuid.UUID, step_id: int, request_id: uuid.UUID | None) -> None:
    """Record that a running step waits (on `request_id`, for an agent node)."""
    with store_session(data_store_id) as db:
        db.execute(
            update(WorkflowRunStep)
            .where(
                WorkflowRunStep.id == step_id, WorkflowRunStep.status == WorkflowStepStatus.running
            )
            .values(status=WorkflowStepStatus.waiting, waiting_on_request_id=request_id)
        )
        db.commit()


def trace_metadata(
    run: RunState, agent: AgentNode, agent_run_id: int, path: tuple[str, ...]
) -> dict[str, Any]:
    """Attached to an agent node's trace (Langfuse): an agent run's, plus the workflow's."""
    return tracing.trace_metadata(
        session_id=tracing.run_session(run.organisation_id, agent_run_id),
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

    Run again for the same node (its process stopped part way, and the run service retried it),
    a turn an earlier attempt began carries on from the run's last checkpoint without the
    message being given twice, and one that ended gives what it ended with.
    """
    # Imported here: the agents API imports this module.
    from aios.api.agents import (
        Continue,
        Resume,
        _begin_turn,
        _mark_resumed,
        _record_turn,
        _resume_command,
        _start_turn,
    )
    from aios.api.agents.chat import _NOTHING, _continue_input

    config = {
        **checkpoints.thread_config(agent_run_id),
        "callbacks": tracing.callbacks(),
        "run_name": "agent-run",
        "metadata": package.trace_metadata(run, agent, agent_run_id, path),
    }
    store_id = run.data_store_id
    async with checkpoints.turn(store_id, agent_run_id) as saver:
        graph = build_agent(
            model=agent.model,
            instructions=agent.instructions,
            tools=agent.tools,
            checkpointer=saver,
        )
        resume = Resume()
        latest = await asyncio.to_thread(_latest_turn, store_id, agent_run_id)
        turn_id = None
        if latest is not None and latest.status == AgentRunStatus.running:
            # Begun by an earlier attempt that stopped part way: carried on.
            turn_id = latest.id
            graph_input = await _continue_input(graph, config, Continue(HumanMessage(text)))
            if graph_input is _NOTHING:
                graph_input = _SKIP
        elif resuming:
            # None if none of its requests has been decided: it goes on waiting.
            graph_input = await _resume_command(graph, config, store_id, agent_run_id, resume)
            if graph_input is None:
                graph_input = _SKIP
        elif latest is not None:
            # Ended in an earlier attempt, which stopped before recording how the node went.
            if latest.error is not None:
                raise RunError(latest.error)
            graph_input = _SKIP
        else:
            graph_input = {"messages": [HumanMessage(text)]}
        if graph_input is _SKIP and turn_id is not None:
            # Begun, but the graph had already got to its end: the turn ends here.
            await _record_turn(
                graph, config, store_id, agent_run_id, turn_id=turn_id, error=None, partial=None
            )
        if graph_input is _SKIP and resume.dealt_with:
            # Decided, but no call waits on them any more (given just before a worker died,
            # say): they count as given, rather than having the run queued again for them.
            await asyncio.to_thread(_mark_resumed, store_id, agent_run_id, resume.dealt_with)
        if graph_input is not _SKIP:
            if turn_id is not None:
                pass  # carrying on the turn an earlier attempt began
            elif resuming:
                turn_id = await asyncio.to_thread(_begin_turn, store_id, agent_run_id)
            else:
                turn_id = await asyncio.to_thread(_start_turn, store_id, agent_run_id, text)
            error: str | None = "The turn was stopped."
            # Cancelled with its run service item (its process stopping, or its run's time
            # running out): the turn stays running, for a later attempt to carry on, or for
            # whoever ended the run to fail.
            cancelled = False
            # Charged to the turn, and capped by its caps and its workflow run's
            # (`aios.usage`).
            charge = None
            if turn_id is not None:
                charge = await asyncio.to_thread(usage.for_turn, store_id, turn_id)
            limit = usage.recursion_limit(charge)
            try:
                with usage.charging(charge):
                    await graph.ainvoke(
                        graph_input,
                        config if limit is None else {**config, "recursion_limit": limit},
                        context=ChatDeps(
                            user_id=run.created_by,
                            run_id=agent_run_id,
                            data_store_id=run.data_store_id,
                        ),
                        # As in a chat turn: checkpointed step by step, so a failed turn's
                        # history is kept.
                        durability="sync",
                    )
                error = None
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                error = error_text(exc)
                raise
            finally:
                with CancelScope(shield=True):
                    if resume.dealt_with:
                        # Given to the run, which takes messages again unless another waits.
                        await asyncio.to_thread(
                            _mark_resumed, store_id, agent_run_id, resume.dealt_with
                        )
                    # The message count for listings, and, if the turn failed, a thread the user
                    # can carry on from on the run's page (see `_keep_unfinished_turn`).
                    if not cancelled:
                        await _record_turn(
                            graph,
                            config,
                            store_id,
                            agent_run_id,
                            turn_id=turn_id,
                            error=error,
                            partial=None,
                        )
        state = await graph.aget_state(config)
    request_id = _waits_on(state)
    if request_id is not None:
        raise NodeWaiting(request_id)
    return state.values["messages"][-1].text


# Nothing (left) for a turn to run.
_SKIP = object()


@dataclass(frozen=True)
class _Turn:
    id: int
    status: AgentRunStatus
    error: str | None


def _latest_turn(data_store_id: uuid.UUID, agent_run_id: int) -> _Turn | None:
    with store_session(data_store_id, "ro") as store:
        row = store.execute(
            select(Turn.id, Turn.status, Turn.error)
            .where(Turn.agent_run_id == agent_run_id)
            .order_by(Turn.id.desc())
            .limit(1)
        ).one_or_none()
    return None if row is None else _Turn(*row)


def _waits_on(state) -> uuid.UUID | None:
    """The schema change request an agent run's state waits on, if it waits on one (the first,
    if on several)."""
    from aios.tools.schema_change import REQUEST_KEY

    for waiting in state.interrupts:
        if isinstance(waiting.value, dict) and REQUEST_KEY in waiting.value:
            return uuid.UUID(waiting.value[REQUEST_KEY])
    return None
