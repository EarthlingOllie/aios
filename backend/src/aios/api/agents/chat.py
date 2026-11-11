"""A run's chat stream: one turn of the agent, as the chat endpoint, a schedule, a chat's
mention and a resumed run each run it."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from sqlalchemy import func, select, true, update
from sqlalchemy.orm import Session

from aios import checkpoints, invocations, tracing, turn_lease, ui_stream

# Read on the package at call time, where tests replace them.
from aios.api import agents as package
from aios.api.agents.common import refuse_if_archived
from aios.api.agents.runs import build_version_tools, get_live_run, get_org_run
from aios.api.schema_changes import waiting_on
from aios.auth import CurrentOrganisation, CurrentUser
from aios.datastores import data_store_of, history, schema_changes
from aios.db import DB, DbId, SessionLocal
from aios.models import (
    Agent,
    AgentRun,
    AgentRunStatus,
    Organisation,
    SchemaChangeRequest,
    SchemaChangeStatus,
)
from aios.runner import build_agent, run_title
from aios.sandbox.sessions import (
    SandboxNotConfigured,
    manager,
    worth_prewarming,
)
from aios.tools import (
    AgentTool,
    stored_settings,
)

log = logging.getLogger(__package__)

router = APIRouter(prefix="/api")


def trace_metadata(run: AgentRun) -> dict[str, Any]:
    """Attached to the run's trace (Langfuse), e.g. for filtering and experiments: the agent's
    organisation, which the run executes for, and the member who started it."""
    version = run.agent_version
    return tracing.trace_metadata(
        session_id=checkpoints.thread_id(run.id),
        organisation_id=run.agent.organisation_id,
        created_by=run.created_by,
        tags=[f"agent:{run.agent.name}", f"agent-version:{version.version}"],
        agent_run_id=run.id,
        **tracing.agent_version_metadata(version),
    )


@dataclass(frozen=True)
class ChatContext:
    """Everything a chat turn needs from the database, as plain values."""

    model: str | None
    instructions: str
    metadata: dict[str, Any]
    # The version's tools, built from their stored settings.
    tools: list[AgentTool] = field(default_factory=list)
    # The `run_python` tool's allowed domains, if the version has the tool and the run's
    # sandbox is worth pre-warming for this turn (see `worth_prewarming`).
    sandbox_domains: list[str] | None = None
    # The data store the turn's tools use: the agent's organisation's.
    data_store_id: uuid.UUID | None = None
    # The member who started the run, which the turn's tools are told (`ChatDeps.user_id`);
    # None for a schedule's.
    created_by: int | None = None


def load_chat_context(
    db: Session, agent_id: int, run_id: int, *, org: Organisation | None = None
) -> ChatContext:
    """What a turn of the run needs. The turn executes for the agent's organisation, whoever
    asks for it: its store is the organisation's, and its trace and tools are told who started
    the run. `org` is the organisation of the request asking for the turn, which the run's agent
    must be in (a 404 otherwise); the scheduler, a chat's mention and a resumed run are let in
    by what started them, and pass none."""
    run = (
        get_live_run(db, agent_id, run_id)
        if org is None
        else get_org_run(db, org, agent_id, run_id)
    )
    if org is not None:
        # A new turn asked for by hand, which an archived agent doesn't start. What started the
        # other turns checked when it started their runs, and they finish as already going.
        refuse_if_archived(run.agent)
    version = run.agent_version
    tools = build_version_tools(version)
    # Parsed as the tool itself is built (`build_tool`), so a pre-warmed sandbox's proxy token
    # carries the domains the tool expects.
    sandbox_domains = next(
        (
            stored_settings(t.tool_key, t.config).allowed_domains
            for t in version.tools
            if t.tool_key == "run_python"
        ),
        None,
    )
    if sandbox_domains is not None and not worth_prewarming(
        db, run.id, first_turn=run.message_count == 0
    ):
        sandbox_domains = None
    # The agent's organisation's, on this session rather than one of its own alongside it;
    # committed in case it's created.
    data_store_id = data_store_of(db, Agent, agent_id)
    db.commit()
    return ChatContext(
        model=version.model,
        instructions=version.instructions,
        metadata=trace_metadata(run),
        tools=tools,
        sandbox_domains=sandbox_domains,
        data_store_id=data_store_id,
        created_by=run.created_by,
    )


def _start_turn(run_id: int, prompt: str, *, heartbeat: bool = False) -> None:
    """Mark the run running as a turn begins, and title it after its first message.

    With `heartbeat`, the turn runs outside a request and its process will keep the run's
    heartbeat (`aios.firing`); without, any earlier one is cleared, so a request's turn isn't
    failed as stalled."""
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                title=func.coalesce(AgentRun.title, run_title(prompt)),
                status=AgentRunStatus.running,
                started_at=func.now(),
                finished_at=None,
                heartbeat_at=func.now() if heartbeat else None,
            )
        )
        db.commit()


def tool_call_count(response: AIMessage) -> int:
    """The tool calls in a model response: the agent's, and the provider's own (e.g. web
    search), which are content blocks of the response (as the run page counts them)."""
    blocks = response.content if isinstance(response.content, list) else []
    server = sum(isinstance(b, dict) and b.get("type") == "server_tool_use" for b in blocks)
    return len(response.tool_calls) + server


def _count_step(run_id: int, response: AIMessage) -> None:
    """Add a model step to the run's counters: its tool calls, and what the call cost."""
    usage = response.usage_metadata or {}
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                tool_call_count=func.coalesce(AgentRun.tool_call_count, 0)
                + tool_call_count(response),
                input_tokens=func.coalesce(AgentRun.input_tokens, 0) + usage.get("input_tokens", 0),
                output_tokens=func.coalesce(AgentRun.output_tokens, 0)
                + usage.get("output_tokens", 0),
            )
        )
        db.commit()


def _end_turn(run_id: int, *, failed: bool, message_count: int | None = None) -> None:
    """Record how the turn ended; `message_count` is left as it was if the turn never started."""
    values: dict[str, Any] = {
        "status": AgentRunStatus.failed if failed else AgentRunStatus.succeeded,
        "finished_at": func.now(),
        "updated_at": func.now(),
    }
    if message_count is not None:
        values["message_count"] = message_count
    with SessionLocal() as db:
        # Every turn ends here (chats, schedules, workflow agent nodes), however it ended: with
        # the run's own update, it goes in the run history export's outbox. The outbox row
        # first, as deleting a run does, so the two never each hold the row the other wants.
        history.enqueue_runs(db, AgentRun.id == run_id)
        # A no-op if the run was deleted mid-turn.
        db.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))
        db.commit()


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


def _cancel_requests(run_id: int, tool_call_ids: list[str]) -> None:
    """Cancel the pending schema change requests these tool calls made: the model has been told
    the calls didn't finish, so nothing waits on them any more."""
    if not tool_call_ids:
        return
    with SessionLocal() as db:
        schema_changes.cancel(
            db,
            SchemaChangeRequest.requested_by_run == run_id,
            SchemaChangeRequest.tool_call_id.in_(tool_call_ids),
        )
        db.commit()


async def _record_turn(agent, config, run_id: int, *, finished: bool, partial) -> None:
    if not finished:
        try:
            stopped = await _keep_unfinished_turn(agent, config, partial)
            await run_in_threadpool(_cancel_requests, run_id, stopped)
        except Exception:
            # E.g. a stopped turn, whose lease is gone: it can't write the run's history.
            log.exception("couldn't keep the unfinished turn of run %s", run_id)
    try:
        state = await agent.aget_state(config)
        await run_in_threadpool(
            package._end_turn,
            run_id,
            failed=not finished,
            message_count=len(state.values.get("messages", [])),
        )
    except Exception:
        log.exception("couldn't record the end of a turn of run %s", run_id)


# Pre-warming tasks, kept so they aren't garbage-collected before they finish.
_prewarming: set[asyncio.Task] = set()


def prewarm_sandbox(run_id: int, allowed_domains: list[str]) -> None:
    """Starts the run's sandbox in the background, so it's ready by the model's first call."""

    async def ensure() -> None:
        try:
            await manager.ensure(run_id=run_id, allowed_domains=allowed_domains)
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


def _decided_requests(run_id: int) -> dict[str, dict[str, Any]]:
    """The run's decided requests it hasn't been given yet, with their outcomes, by id."""
    with SessionLocal() as db:
        requests = db.scalars(
            select(SchemaChangeRequest).where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.resumed_at.is_(None),
            )
        ).all()
        return {str(r.id): schema_changes.outcome(r) for r in requests if r.status.decided}


async def _resume_command(agent, config, run_id: int, resume: Resume) -> Command | None:
    """The command resuming the run's waiting tool calls whose requests are decided (by
    interrupt id, so others keep waiting); None if none are."""
    from aios.tools.schema_change import REQUEST_KEY

    state = await agent.aget_state(config)
    waiting = {
        i.id: i.value[REQUEST_KEY]
        for i in state.interrupts
        if isinstance(i.value, dict) and REQUEST_KEY in i.value
    }
    outcomes = await run_in_threadpool(_decided_requests, run_id)
    # Those decided after this aren't dealt with: their own resume gives them to the run.
    resume.dealt_with = [uuid.UUID(rid) for rid in outcomes]
    values = {iid: outcomes[rid] for iid, rid in waiting.items() if rid in outcomes}
    return Command(resume=values) if values else None


def _check_not_waiting(run_id: int) -> None:
    with SessionLocal() as db:
        if waiting_on(db, run_id):
            raise RunWaiting


async def turn_stream(
    ctx: ChatContext,
    run_id: int,
    turn: HumanMessage | Resume,
    lease: turn_lease.Lease | None = None,
) -> AsyncIterator[ui_stream.Chunk]:
    """One turn of a run, as AI SDK UI message chunks: the user's message is checkpointed before
    the model is called, and whatever the reply got to is kept however the turn ends (it
    completes, fails, or the consumer stops it). A failure is reported as an `error` chunk.

    `turn` is the user's message, or `Resume` to carry on a run that waits on schema change
    requests now they're decided; a message to a run still waiting is refused (an `error`
    chunk), checked once the turn has the run to itself.

    The turn holds the run's turn lease (`checkpoints.turn`): `lease`, if the caller took it
    already, else it waits for it, and reports a run that stays busy as an `error` chunk.

    Shared by the chat endpoint, the scheduler (`aios.scheduler`) and `resume_run`."""
    config = {
        **checkpoints.thread_config(run_id),
        "callbacks": tracing.callbacks(),
        "run_name": "agent-run",
        "metadata": ctx.metadata,
    }
    ui = ui_stream.UIStream()
    for chunk in ui.start():
        yield chunk
    if ctx.sandbox_domains is not None:
        prewarm_sandbox(run_id, ctx.sandbox_domains)
    try:
        # Waits for any other turn of this run to finish (see `checkpoints.turn`).
        async with checkpoints.turn(run_id, lease) as saver:
            agent = build_agent(
                model=ctx.model,
                instructions=ctx.instructions,
                tools=ctx.tools,
                checkpointer=saver,
            )
            if isinstance(turn, Resume):
                graph_input = await _resume_command(agent, config, run_id, turn)
            else:
                await run_in_threadpool(_check_not_waiting, run_id)
                graph_input = {"messages": [turn]}
            if graph_input is not None:
                # Closed here if the consumer stops the turn, so the turn is recorded while the
                # checkpointer's connection is still open.
                turn_chunks = _run_turn(agent, config, ctx, ui, run_id, graph_input, saver.lease)
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
                await run_in_threadpool(package._end_turn, run_id, failed=True)
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
    graph_input,
    lease: turn_lease.Lease,
) -> AsyncIterator[ui_stream.Chunk]:
    finished = False
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
            # One small update per model step, so the run's counters are live.
            for response in model_responses(mode, data):
                await run_in_threadpool(_count_step, run_id, response)
        if producer.cancelled():
            raise _Stopped
        if (exc := producer.exception()) is not None:
            raise exc
        finished = True
    except Exception as exc:
        if not isinstance(exc, _Stopped):
            log.exception("agent run %s failed", run_id)
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
                agent, config, run_id, finished=finished, partial=ui.partial_reply()
            )


def _run_agent(run_id: int) -> int | None:
    """The run's agent; None if the run is gone."""
    with SessionLocal() as db:
        return db.scalar(select(AgentRun.agent_id).where(AgentRun.id == run_id))


def _mark_resumed(run_id: int, request_ids: list[uuid.UUID] | None = None) -> None:
    """The run has been given these requests' outcomes (None: all its decided ones'): it takes
    new messages again, unless another still waits."""
    with SessionLocal() as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(
                SchemaChangeRequest.requested_by_run == run_id,
                SchemaChangeRequest.resumed_at.is_(None),
                SchemaChangeRequest.status.not_in(
                    [SchemaChangeStatus.pending, SchemaChangeStatus.approved]
                ),
                true() if request_ids is None else SchemaChangeRequest.id.in_(request_ids),
            )
            .values(resumed_at=func.now())
        )
        db.commit()


class ResumeFailed(RuntimeError):
    """A resume that couldn't give the run its outcomes (it never got the run to itself): worth
    trying again."""


async def resume_run(run_id: int) -> None:
    """Carry on a run that waits on schema change requests, now some are decided: their tool
    calls return the outcomes, and the turn goes on from there, for the agent's organisation as
    any turn of the run is. The rest of the turn is in the run's history, for its chat to show.

    Raises `ResumeFailed` if the turn couldn't start (the run stays waiting)."""
    agent_id = await run_in_threadpool(_run_agent, run_id)
    if agent_id is None:
        return
    try:
        with SessionLocal() as db:
            ctx = await run_in_threadpool(package.load_chat_context, db, agent_id, run_id)
    except HTTPException as exc:
        # Its version can't run any more (a tool left the library, say): no turn of it can, so
        # it isn't left waiting on what can't come.
        log.warning("agent run %s can't be resumed: %s", run_id, exc.detail)
        await run_in_threadpool(_mark_resumed, run_id)
        await run_in_threadpool(_finish_invocation, run_id, str(exc.detail))
        return
    resume = Resume()
    error = None
    async for chunk in turn_stream(ctx, run_id, resume):
        if chunk["type"] == "error":
            error = chunk["errorText"]
            log.warning("resuming agent run %s: %s", run_id, error)
    if resume.dealt_with is None:
        raise ResumeFailed(f"Agent run {run_id} couldn't be resumed")
    await run_in_threadpool(_mark_resumed, run_id, resume.dealt_with)
    await run_in_threadpool(_finish_invocation, run_id, error)


def _finish_invocation(run_id: int, error: str | None) -> None:
    """Post the resumed run's reply to the chat whose mention started it, if one did."""
    with SessionLocal() as db:
        invocations.finish_resumed(db, run_id, error)
        db.commit()


@router.post("/agents/{agent_id}/runs/{run_id}/chat")
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
    # session is function-scoped (see `aios.db.DB`), so it's closed and its connection returned
    # to the pool before the response streams; the saves use sessions of their own.
    ctx = await run_in_threadpool(package.load_chat_context, db, agent_id, run_id, org=org)
    message = request.messages[-1]
    if message.role != "user":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Expected a user message")
    prompt = ui_stream.human_message(message)
    try:
        await run_in_threadpool(_check_not_waiting, run_id)
    except RunWaiting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, WAITING) from exc
    # The run's turn lease, before the response starts, so a run that stays busy past the wait
    # (another tab's turn, a schedule's) can still be answered 409.
    try:
        lease = await turn_lease.acquire(run_id)
    except turn_lease.RunBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, turn_lease.BUSY) from exc
    except turn_lease.RunGone as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from exc
    try:
        await run_in_threadpool(package._start_turn, run_id, prompt.text)
    except BaseException:
        with CancelScope(shield=True):
            await turn_lease.release(lease)
        raise

    return ui_stream.streaming_response(turn_stream(ctx, run_id, prompt, lease))
