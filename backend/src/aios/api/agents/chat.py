"""A run's chat stream: one turn of the agent, as the chat endpoint, a schedule, a chat's
mention and a resumed run each run it."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session

from aios import checkpoints, invocations, search_index, tracing, turn_lease, turns, ui_stream

# Read on the package at call time, where tests replace them.
from aios.api import agents as package
from aios.api.agents.common import refuse_if_archived
from aios.api.agents.runs import build_version_tools
from aios.api.schema_changes import of_run, waiting_on
from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import data_store_for, schema_changes, store_session
from aios.datastores.models import Agent, AgentRun, AgentVersion, SchemaChangeRequest
from aios.db import DB, DbId, SessionLocal
from aios.models import (
    DataStore,
    SchemaChangeStatus,
)
from aios.permissions import Action, allow
from aios.runner import build_agent, run_title
from aios.sandbox.sessions import (
    SandboxNotConfigured,
    manager,
    worth_prewarming,
)
from aios.tools import (
    AgentTool,
    ToolOrganisation,
    stored_settings,
)

log = logging.getLogger(__package__)

router = APIRouter(prefix="/api")


def trace_metadata(
    organisation_id: int, run: AgentRun, agent: Agent, version: AgentVersion
) -> dict[str, Any]:
    """Attached to the run's trace (Langfuse), e.g. for filtering and experiments: the agent's
    organisation, which the run executes for, and the member who started it. `run`, `agent` and
    `version` are from the organisation's store."""
    return tracing.trace_metadata(
        session_id=tracing.run_session(organisation_id, run.id),
        organisation_id=organisation_id,
        created_by=run.created_by,
        tags=[f"agent:{agent.name}", f"agent-version:{version.version}"],
        agent_run_id=run.id,
        **tracing.agent_version_metadata(version),
    )


@dataclass(frozen=True)
class ChatContext:
    """Everything a chat turn needs from the database, as plain values."""

    # The data store the run is in, and its tools use: the agent's organisation's.
    data_store_id: uuid.UUID
    model: str | None
    instructions: str
    metadata: dict[str, Any]
    # The version's tools, built from their stored settings.
    tools: list[AgentTool] = field(default_factory=list)
    # The `run_python` tool's allowed domains, if the version has the tool and the run's
    # sandbox is worth pre-warming for this turn (see `worth_prewarming`).
    sandbox_domains: list[str] | None = None
    # The member who started the run, which the turn's tools are told (`ChatDeps.user_id`);
    # None for a schedule's.
    created_by: int | None = None


def load_chat_context(
    db: Session, data_store_id: uuid.UUID, agent_id: int, run_id: int, *, by_hand: bool = False
) -> ChatContext:
    """What a turn of the store's run needs (the agent's id is checked against the run's: a 404
    otherwise). The turn executes for the agent's organisation, whoever asks for it: its store
    is the organisation's, and its trace and tools are told who started the run. `by_hand` for a
    new turn a member asks for, which an archived agent doesn't start; the scheduler, a chat's
    mention and a resumed run are let in by what started them.

    `db` is the caller's platform session, committed (and closed) here, so it holds no
    connection while the store is read."""
    organisation_id = db.scalar(
        select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
    )
    db.commit()
    db.close()
    if organisation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # The run, and the agent and version it pinned, from the organisation's store.
    with store_session(data_store_id, "ro") as store:
        run = store.get(AgentRun, run_id)
        if run is None or run.agent_id != agent_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        agent = store.get_one(Agent, run.agent_id)
        version = store.get_one(AgentVersion, run.agent_version_id)
        if by_hand:
            # What started the other turns checked when it started their runs, and they finish
            # as already going.
            refuse_if_archived(agent)
        tools = build_version_tools(version, ToolOrganisation(organisation_id, store))
        # Parsed as the tool itself is built (`build_tool`), so a pre-warmed sandbox's proxy
        # token carries the domains the tool expects.
        sandbox_domains = next(
            (
                stored_settings(t.tool_key, t.config).allowed_domains
                for t in version.tools
                if t.tool_key == "run_python"
            ),
            None,
        )
        metadata = trace_metadata(organisation_id, run, agent, version)
        model, instructions = version.model, version.instructions
        if sandbox_domains is not None and not worth_prewarming(
            store, run.id, first_turn=run.message_count == 0
        ):
            sandbox_domains = None
    return ChatContext(
        data_store_id=data_store_id,
        model=model,
        instructions=instructions,
        metadata=metadata,
        tools=tools,
        sandbox_domains=sandbox_domains,
        created_by=run.created_by,
    )


def _start_turn(
    data_store_id: uuid.UUID, run_id: int, prompt: str, *, heartbeat: bool = False
) -> int | None:
    """Begin a turn of the store's run (`aios.turns.begin`), and title the run after its first
    message; the turn's id (None if the run is gone). Clears the run's old heartbeat, unless
    `heartbeat`."""
    with store_session(data_store_id) as store:
        store.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                title=func.coalesce(AgentRun.title, run_title(prompt)),
                heartbeat_at=func.now() if heartbeat else None,
                updated_at=func.now(),
            )
        )
        turn_id = turns.begin(store, run_id)
        store.commit()
    return turn_id


def tool_call_count(response: AIMessage) -> int:
    """The tool calls in a model response: the agent's, and the provider's own (e.g. web
    search), which are content blocks of the response (as the run page counts them)."""
    blocks = response.content if isinstance(response.content, list) else []
    server = sum(isinstance(b, dict) and b.get("type") == "server_tool_use" for b in blocks)
    return len(response.tool_calls) + server


def _count_step(data_store_id: uuid.UUID, turn_id: int, response: AIMessage) -> None:
    """Add a model call to the turn's counters: its tool calls, and what the call cost."""
    usage = response.usage_metadata or {}
    with store_session(data_store_id) as store:
        turns.count(
            store,
            turn_id,
            tool_calls=tool_call_count(response),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        store.commit()


def _end_turn(
    data_store_id: uuid.UUID,
    run_id: int,
    turn_id: int | None,
    *,
    error: str | None,
    messages: Sequence[BaseMessage] | None = None,
) -> None:
    """Record how the turn of the store's run ended (`error` None if it went fine), and finish
    the run's invocation if no chat follows it (a chat's posts the reply itself). `messages` is
    the run's conversation as the turn left it: its count is the run's, and what the turn added
    to it (since the run's last count) is indexed for search (`aios.search_index`), in the same
    transaction; None if the turn never started (both are left as they were). `turn_id` None for
    a turn that never began.

    Every turn ends here (chats, schedules, workflow agent nodes), however it ended."""
    values: dict[str, Any] = {"updated_at": func.now()}
    if messages is not None:
        values["message_count"] = len(messages)
    with store_session(data_store_id) as store:
        # Locked, so a turn ending at once in another process doesn't index the same messages.
        # None if the run was deleted mid-turn: then this is a no-op.
        before = store.scalar(
            select(AgentRun.message_count).where(AgentRun.id == run_id).with_for_update()
        )
        if before is not None:
            store.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
        if turn_id is not None:
            turns.end(store, turn_id, error)
            if before is not None:
                invocations.finish_quietly(store, run_id, error)
                if messages is not None:
                    search_index.index_turn(store, run_id, turn_id, messages, before)
        store.commit()


def model_responses(mode: str, data: Any) -> list[AIMessage]:
    """The model's finished responses in one item of the agent's stream: each is a step."""
    if mode != "updates":
        return []
    return [
        message
        for node, update in data.items()
        if node == "model"
        for message in (update or {}).get("messages", [])
        if isinstance(message, AIMessage)
    ]


async def _keep_unfinished_turn(agent, config, partial: AIMessage | None) -> list[str]:
    """Record a turn that failed or was stopped, so the next turn sees what the user saw.

    The user's message is already in the thread (it's checkpointed before the model is called).
    What's added: the reply's text so far, and, if it stopped while tools were running, a result
    for each call still waiting for one (a call without a result can't be sent to the model).
    Returns the ids of those calls.
    """
    state = await agent.aget_state(config)
    messages = state.values.get("messages", [])
    last = messages[-1] if messages else None
    # The model's last message, and the results of its tool calls so far.
    i = len(messages)
    while i and isinstance(messages[i - 1], ToolMessage):
        i -= 1
    calls, results = (messages[i - 1] if i else None), messages[i:]
    answered = {m.tool_call_id for m in results}
    stopped = [
        ToolMessage(
            "The turn ended before this tool call finished.",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
        for call in (calls.tool_calls if isinstance(calls, AIMessage) else [])
        if call["id"] not in answered
    ]
    if isinstance(calls, AIMessage) and calls.tool_calls:
        # Written even if every call has a result: the model's message and the results can be
        # only pending writes of the step the turn ended in, which an update's checkpoint doesn't
        # carry over. By id, so those already checkpointed aren't repeated.
        await agent.aupdate_state(
            config, {"messages": [calls, *results, *stopped]}, as_node="tools"
        )
    # A reply to the user's message or to tool results: its message isn't in the state until the
    # model has finished it.
    if partial is not None and isinstance(last, HumanMessage | ToolMessage):
        await agent.aupdate_state(config, {"messages": [partial]}, as_node="model")
    return [m.tool_call_id for m in stopped]


def _cancel_requests(data_store_id: uuid.UUID, run_id: int, tool_call_ids: list[str]) -> None:
    """Cancel the pending schema change requests these tool calls of the store's run made: the
    model has been told the calls didn't finish, so nothing waits on them any more."""
    if not tool_call_ids:
        return
    with store_session(data_store_id) as store:
        cancelled = schema_changes.cancel(
            store, *of_run(run_id), SchemaChangeRequest.tool_call_id.in_(tool_call_ids)
        )
        store.commit()
    schema_changes.clear_notifications_of(cancelled)


async def _record_turn(
    agent,
    config,
    data_store_id: uuid.UUID,
    run_id: int,
    *,
    turn_id: int | None,
    error: str | None,
    partial,
) -> None:
    """Record the end of the run's turn: `error` None if it finished, else why not (and what it
    got to is kept, `_keep_unfinished_turn`)."""
    finished = error is None
    if not finished:
        try:
            stopped = await _keep_unfinished_turn(agent, config, partial)
            await run_in_threadpool(_cancel_requests, data_store_id, run_id, stopped)
        except Exception:
            # E.g. a stopped turn, whose lease is gone: it can't write the run's history.
            log.exception("couldn't keep the unfinished turn of run %s", run_id)
    try:
        state = await agent.aget_state(config)
        await run_in_threadpool(
            package._end_turn,
            data_store_id,
            run_id,
            turn_id,
            error=error,
            messages=state.values.get("messages", []),
        )
    except Exception:
        log.exception("couldn't record the end of a turn of run %s", run_id)


# Pre-warming tasks, kept so they aren't garbage-collected before they finish.
_prewarming: set[asyncio.Task] = set()


def prewarm_sandbox(data_store_id: uuid.UUID, run_id: int, allowed_domains: list[str]) -> None:
    """Starts the run's sandbox in the background, so it's ready by the model's first call."""

    async def ensure() -> None:
        try:
            await manager.ensure(
                data_store_id=data_store_id, run_id=run_id, allowed_domains=allowed_domains
            )
        except SandboxNotConfigured:
            pass
        except Exception:
            # The tool call will try again, and report a failure to the model.
            log.exception("couldn't pre-warm the sandbox for run %s", run_id)

    task = asyncio.create_task(ensure())
    _prewarming.add(task)
    task.add_done_callback(_prewarming.discard)


# What a new message to a run waiting on a schema change request is told.
WAITING = (
    "This run is waiting for a decision on its schema change request: approve or reject it"
    " first, then carry on."
)


class RunWaiting(Exception):
    """A new message to a run that waits on a schema change request."""

    def __init__(self) -> None:
        super().__init__(WAITING)


@dataclass
class Resume:
    """A turn that resumes a run waiting on schema change requests, giving each waiting tool
    call its request's outcome, for those decided (`resume_run`). The turn fills in which
    requests it dealt with, once it has the run to itself."""

    # The decided requests (not yet resumed) it found: given to their calls, or with no call
    # waiting on them any more.
    dealt_with: list[uuid.UUID] | None = None


@dataclass
class Continue:
    """A turn that carries on one an earlier attempt began and never finished (its process
    stopped part way; `aios.run_service`): from the run's last checkpoint, without giving the
    run its message again. `message` is the turn's, for a run that never got as far as saving
    it."""

    message: HumanMessage


# A turn with nothing (left) to run.
_NOTHING = object()


async def _continue_input(agent, config, turn: Continue):
    """What carrying on the run takes: its message, if the run never saved it; None (carry on
    from the last checkpoint) if the graph stopped part way; `_NOTHING` if it had finished, or
    waits on a schema change request (its resume carries it on)."""
    state = await agent.aget_state(config)
    if not state.values.get("messages"):
        return {"messages": [turn.message]}
    if state.next and not state.interrupts:
        return None
    return _NOTHING


def _decided_requests(data_store_id: uuid.UUID, run_id: int) -> dict[str, dict[str, Any]]:
    """The store's run's decided requests it hasn't been given yet, with their outcomes, by
    id."""
    with store_session(data_store_id, "ro") as store:
        requests = store.scalars(
            select(SchemaChangeRequest).where(
                *of_run(run_id), SchemaChangeRequest.resumed_at.is_(None)
            )
        ).all()
        return {str(r.id): schema_changes.outcome(r) for r in requests if r.status.decided}


async def _resume_command(
    agent, config, data_store_id: uuid.UUID, run_id: int, resume: Resume
) -> Command | None:
    """The command resuming the run's waiting tool calls whose requests are decided (by
    interrupt id, so others keep waiting); None if none are."""
    from aios.tools.schema_change import REQUEST_KEY

    state = await agent.aget_state(config)
    waiting = {
        i.id: i.value[REQUEST_KEY]
        for i in state.interrupts
        if isinstance(i.value, dict) and REQUEST_KEY in i.value
    }
    outcomes = await run_in_threadpool(_decided_requests, data_store_id, run_id)
    # Those decided after this aren't dealt with: their own resume gives them to the run.
    resume.dealt_with = [uuid.UUID(rid) for rid in outcomes]
    values = {iid: outcomes[rid] for iid, rid in waiting.items() if rid in outcomes}
    return Command(resume=values) if values else None


def _check_not_waiting(data_store_id: uuid.UUID, run_id: int) -> None:
    with store_session(data_store_id, "ro") as store:
        if waiting_on(store, run_id):
            raise RunWaiting


async def turn_stream(
    ctx: ChatContext,
    run_id: int,
    turn: HumanMessage | Resume | Continue,
    lease: turn_lease.Lease | None = None,
    *,
    turn_id: int | None = None,
) -> AsyncIterator[ui_stream.Chunk]:
    """One turn of a run (in the store `ctx.data_store_id`), as AI SDK UI message chunks: the
    user's message is checkpointed before the model is called, and whatever the reply got to is
    kept however the turn ends (it completes, fails, or the consumer stops it). A failure is
    reported as an `error` chunk.

    `turn` is the user's message, or `Resume` to carry on a run that waits on schema change
    requests now they're decided; a message to a run still waiting is refused (an `error`
    chunk), checked once the turn has the run to itself.

    The turn holds the run's turn lease (`checkpoints.turn`): `lease`, if the caller took it
    already, else it waits for it, and reports a run that stays busy as an `error` chunk.

    Its record (`aios.turns`) is `turn_id`, if the caller began it (`_start_turn`), else it
    begins one once it has the run and something to run (a resume with nothing decided to give
    the run isn't a turn).

    Shared by the chat endpoint, the run service (`aios.run_service`) and `resume_run`."""
    config = {
        **checkpoints.thread_config(run_id),
        "callbacks": tracing.callbacks(),
        "run_name": "agent-run",
        "metadata": ctx.metadata,
    }
    ui = ui_stream.UIStream()
    for chunk in ui.start():
        yield chunk
    store_id = ctx.data_store_id
    if ctx.sandbox_domains is not None:
        prewarm_sandbox(store_id, run_id, ctx.sandbox_domains)
    try:
        # Waits for any other turn of this run to finish (see `checkpoints.turn`).
        async with checkpoints.turn(store_id, run_id, lease) as saver:
            agent = build_agent(
                model=ctx.model,
                instructions=ctx.instructions,
                tools=ctx.tools,
                checkpointer=saver,
            )
            if isinstance(turn, Resume):
                graph_input = await _resume_command(agent, config, store_id, run_id, turn)
                if graph_input is None:
                    graph_input = _NOTHING
            elif isinstance(turn, Continue):
                graph_input = await _continue_input(agent, config, turn)
            else:
                await run_in_threadpool(_check_not_waiting, store_id, run_id)
                graph_input = {"messages": [turn]}
            if graph_input is _NOTHING and turn_id is not None:
                # Begun, but the graph had already got to its end: the turn ends here.
                await package._record_turn(
                    agent, config, store_id, run_id, turn_id=turn_id, error=None, partial=None
                )
            elif graph_input is not _NOTHING:
                if turn_id is None:
                    turn_id = await run_in_threadpool(_begin_turn, store_id, run_id)
                # Closed here if the consumer stops the turn, so the turn is recorded while the
                # checkpointer's connection is still open.
                turn_chunks = _run_turn(
                    agent, config, ctx, ui, run_id, turn_id, graph_input, saver.lease
                )
                async with aclosing(turn_chunks):
                    async for chunk in turn_chunks:
                        yield chunk
    except Exception as exc:
        # The turn couldn't start: the run waits on schema change requests, or stayed busy, or
        # e.g. the database or the model's configuration failed.
        if not isinstance(exc, RunWaiting | turn_lease.RunBusy):
            log.exception("agent run %s couldn't start a turn", run_id)
        with CancelScope(shield=True):
            try:
                await run_in_threadpool(
                    package._end_turn, store_id, run_id, turn_id, error=ui_stream.error_text(exc)
                )
            except Exception:
                log.exception("couldn't record the end of a turn of run %s", run_id)
        for chunk in ui.error(exc):
            yield chunk
    for chunk in ui.finish():
        yield chunk


class _Stopped(Exception):
    """The turn was stopped (`aios.turn_lease.stop`)."""

    def __init__(self) -> None:
        super().__init__(turn_lease.STOPPED)


async def _run_turn(
    agent,
    config,
    ctx: ChatContext,
    ui: ui_stream.UIStream,
    run_id: int,
    turn_id: int | None,
    graph_input,
    lease: turn_lease.Lease,
) -> AsyncIterator[ui_stream.Chunk]:
    error: str | None = "The turn was stopped."
    # The graph runs in a task of its own, which stopping the turn cancels (`lease.on_stop`), so
    # a model or tool call underway ends at once rather than at the turn's next write.
    events = agent.astream(
        graph_input,
        config,
        context=package.ChatDeps(
            user_id=ctx.created_by, run_id=run_id, data_store_id=ctx.data_store_id
        ),
        stream_mode=["messages", "updates"],
        # Checkpoint each step before the next starts, so the user's message is saved before
        # the model is called.
        durability="sync",
    )
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def produce() -> None:
        # Closed however the turn ends, which cancels the graph's running tasks (e.g. a tool
        # call) before the turn is recorded.
        async with aclosing(events):
            async for item in events:
                queue.put_nowait(item)

    producer = asyncio.create_task(produce())
    producer.add_done_callback(lambda _: queue.put_nowait(done))
    lease.on_stop = producer.cancel
    if lease.stopped:
        producer.cancel()
    try:
        while (item := await queue.get()) is not done:
            mode, data = item
            for chunk in ui.feed(mode, data):
                yield chunk
            # One small update per model step, so the run's counters are live. A count that
            # can't be written (the store busy, say) doesn't end the turn.
            for response in model_responses(mode, data):
                if turn_id is None:
                    break
                try:
                    await run_in_threadpool(_count_step, ctx.data_store_id, turn_id, response)
                except Exception:
                    log.exception("couldn't count a step of agent run %s", run_id)
        if producer.cancelled():
            raise _Stopped
        if (exc := producer.exception()) is not None:
            raise exc
        error = None
    except Exception as exc:
        if not isinstance(exc, _Stopped):
            log.exception("agent run %s failed", run_id)
        error = ui_stream.error_text(exc)
        for chunk in ui.error(exc):
            yield chunk
    finally:
        # Also when the client stops the turn, which cancels this task (or closes this stream
        # at a `yield`): the graph's task is cancelled and waited for before the turn is
        # recorded.
        with CancelScope(shield=True):
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            await package._record_turn(
                agent,
                config,
                ctx.data_store_id,
                run_id,
                turn_id=turn_id,
                error=error,
                partial=ui.partial_reply(),
            )


def _begin_turn(data_store_id: uuid.UUID, run_id: int) -> int | None:
    """Begin a turn of the store's run (`aios.turns.begin`); its id."""
    with store_session(data_store_id) as store:
        turn_id = turns.begin(store, run_id)
        store.commit()
    return turn_id


def _run_agent(data_store_id: uuid.UUID, run_id: int) -> int | None:
    """The store's run's agent; None if the run is gone."""
    with store_session(data_store_id, "ro") as store:
        return store.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))


def _mark_resumed(
    data_store_id: uuid.UUID, run_id: int, request_ids: list[uuid.UUID] | None = None
) -> None:
    """The store's run has been given these requests' outcomes (None: all its decided ones'):
    it takes new messages again, unless another still waits."""
    with store_session(data_store_id) as store:
        store.execute(
            update(SchemaChangeRequest)
            .where(
                *of_run(run_id),
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status.not_in(
                    [SchemaChangeStatus.pending, SchemaChangeStatus.approved]
                ),
                true() if request_ids is None else SchemaChangeRequest.id.in_(request_ids),
            )
            .values(resumed_at=func.now())
        )
        store.commit()


class ResumeFailed(RuntimeError):
    """A resume that couldn't give the run its outcomes (it never got the run to itself): worth
    trying again."""


async def resume_run(data_store_id: uuid.UUID, run_id: int) -> None:
    """Carry on the store's run that waits on schema change requests, now some are decided:
    their tool calls return the outcomes, and the turn goes on from there, for the agent's
    organisation as any turn of the run is. The rest of the turn is in the run's history, for
    its chat to show.

    Raises `ResumeFailed` if the turn couldn't start (the run stays waiting)."""
    agent_id = await run_in_threadpool(_run_agent, data_store_id, run_id)
    if agent_id is None:
        return
    try:
        with SessionLocal() as db:
            ctx = await run_in_threadpool(
                package.load_chat_context, db, data_store_id, agent_id, run_id
            )
    except HTTPException as exc:
        # Its version can't run any more (a tool left the library, say): no turn of it can, so
        # it isn't left waiting on what can't come.
        log.warning("agent run %s can't be resumed: %s", run_id, exc.detail)
        await run_in_threadpool(_mark_resumed, data_store_id, run_id)
        await run_in_threadpool(_finish_invocation, data_store_id, run_id, str(exc.detail))
        return
    resume = Resume()
    error = None
    async for chunk in turn_stream(ctx, run_id, resume):
        if chunk["type"] == "error":
            error = chunk["errorText"]
            log.warning("resuming agent run %s: %s", run_id, error)
    if resume.dealt_with is None:
        raise ResumeFailed(f"Agent run {run_id} couldn't be resumed")
    await run_in_threadpool(_mark_resumed, data_store_id, run_id, resume.dealt_with)
    await run_in_threadpool(_finish_invocation, data_store_id, run_id, error)


def _finish_invocation(data_store_id: uuid.UUID, run_id: int, error: str | None) -> None:
    """Post the resumed run's reply to the chat whose mention started it, if one did."""
    with store_session(data_store_id) as store:
        invocations.finish_resumed(store, run_id, error)
        store.commit()


@router.post("/agents/{agent_id}/runs/{run_id}/chat", dependencies=[allow(Action.post)])
async def run_chat(
    agent_id: DbId,
    run_id: DbId,
    body: dict,
    user: CurrentUser,
    db: DB,
    org: CurrentOrganisation,
):
    """Continue a run. The client sends only its new message; history comes from the run's
    LangGraph thread.

    The user's message is checkpointed before the model is called, and whatever the reply got
    to is kept when the turn ends, whether it completes, fails, or is stopped (the client
    disconnecting), so the stored history, which is all the next turn sees, always matches
    what the user saw.
    """
    request = ui_stream.parse_request(body)
    # Sync SQLAlchemy calls go to a worker thread so they don't block the event loop. The `db`
    # session is closed, and its connection returned to the pool, before the response streams;
    # the saves use sessions of their own.
    store_id = await run_in_threadpool(data_store_for, org.id)
    ctx = await run_in_threadpool(
        package.load_chat_context, db, store_id, agent_id, run_id, by_hand=True
    )
    message = request.messages[-1]
    if message.role != "user":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Expected a user message")
    prompt = ui_stream.human_message(message)
    try:
        await run_in_threadpool(_check_not_waiting, store_id, run_id)
    except RunWaiting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, WAITING) from exc
    # The run's turn lease, before the response starts, so a run that stays busy past the wait
    # (another tab's turn, a schedule's) can still be answered 409.
    try:
        lease = await turn_lease.acquire(store_id, run_id)
    except turn_lease.RunBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, turn_lease.BUSY) from exc
    except turn_lease.RunGone as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    try:
        turn_id = await run_in_threadpool(package._start_turn, store_id, run_id, prompt.text)
    except BaseException:
        with CancelScope(shield=True):
            await turn_lease.release(lease)
        raise

    return ui_stream.streaming_response(turn_stream(ctx, run_id, prompt, lease, turn_id=turn_id))
