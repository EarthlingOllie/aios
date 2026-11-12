"""`start_workflow`: an agent hands structured work to a workflow it's allowed to start.

**Settings:** the workflows it may start (`workflow_ids`), and how long a call waits for the run
(`wait_seconds`: 120 by default, at most 600). The tool's description lists those workflows,
read from the organisation as the run is built: each one's id, name, description and input
schema. An archived one is listed as such; a deleted one isn't listed.

**A call** names a workflow (its id, or its name) and gives an input: JSON, or text. It starts
a run of the workflow's current version, its invocation caused by `start_workflow` and chained
to the calling turn's invocation (`aios.invocations.for_run`), started by whoever is behind the
calling turn's (its `requested_by`, the chain's root person; nobody for a schedule's or an
event's). The input is derived text: the run's start nodes get it wrapped as untrusted wherever
it reaches a model (`aios.workflows.advance`), and a subworkflow's input built from it too.

**Refused**, with a reason the model can pass on, and nothing started; each refuses only that
call, so the agent stays usable for its other workflows:

- a workflow that isn't in its allowed list, or has been deleted, or is archived (as is
  anything in its graph);
- one already in the chain of causes (the calling turn's invocation and its ancestors), so
  agents can't loop through workflows; and a chain that would be longer than `chain_max_depth`
  (shared with event triggers, `aios.dispatcher`);
- the workflow's **invocable by** against the role of the chain's root person (`aios.guards`);
  a chain with no person behind it (a schedule's, an event's) was set up by an editor, as was
  the allowed list, so it isn't checked;
- past `start_workflow_rate_per_agent` runs a minute for the agent in the organisation;
- the organisation at its monthly budget, or the chain's root person at their allowance
  (`aios.budgets`; the organisation's budget only, for a chain with no person behind it);
- input that doesn't fit the workflow's input schema, with each error's path and message.

**Results.** The call waits up to its wait time for the run to end, looking every
`start_workflow_poll_seconds`: then it returns the run's output (as JSON, if structured) or
its error. Otherwise it returns the run's id, saying where the result will be posted: the run's
invocation is given a delivery chat (`Invocation.delivery_chat_id`), so when the run ends its
result is handed back there, summarised (`aios.handback`), as a trigger's delivery is. That's
the calling turn's conversation (its invocation's chat, or the nearest ancestor's: a mention's
chat for a workflow's node), else the delivery chat of the trigger behind the chain, else none:
the result is only on the run's page. The run is locked while that's decided, so one that ends
just then is returned rather than lost.

Each call that starts a run is a row in the usage ledger, a `tool_call` charged to the turn.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import guards, organisations, ratelimits, usage
from aios.agent import ChatDeps
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Chat, Invocation, Turn, Workflow, WorkflowRun
from aios.db import SessionLocal
from aios.models import InvocationStatus, UsageKind, WorkflowRunStatus
from aios.tools.data import in_store, in_thread
from aios.tools.registry import AgentTool, ToolOrganisation, ToolSpec, register

DEFAULT_WAIT = 120
MAX_WAIT = 600

NO_TURN = "Workflows can only be started from a turn of an agent run: nothing was started."
NOT_ALLOWED = "You may not start the workflow {workflow}: nothing was started. {yours}"
YOURS = "You may start: {names}."
NONE_ALLOWED = "You may start no workflows."
DELETED = "The workflow {workflow} has been deleted: nothing was started."
ARCHIVED = "The workflow {name} is archived, so it can't be started: nothing was started."
IN_CHAIN = (
    "The workflow {name} is already running in this chain of causes, so starting it would"
    " loop: nothing was started."
)
TOO_DEEP = "The chain of causes would be longer than {most}: nothing was started."
NOT_INVOCABLE = "{reason} Nothing was started."
RATE = "You've started too many workflow runs just now: nothing was started. Try again in a minute."
CANT_START = "The workflow {name} can't be started: {reason} Nothing was started."
WILL_BE_POSTED = "It's still running; its result will be posted in #{chat} when it ends."
RUN_PAGE = "It's still running; its result will be on its run page when it ends."

ENDED = (WorkflowRunStatus.succeeded, WorkflowRunStatus.failed)


class StartWorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_ids: list[int] = Field(
        default_factory=list,
        title="Allowed workflows",
        description="The workflows the agent may start.",
        json_schema_extra={"x-picker": "workflows"},
    )
    wait_seconds: int = Field(
        default=DEFAULT_WAIT,
        ge=0,
        le=MAX_WAIT,
        title="Wait time (seconds)",
        description="How long a call waits for the run before its result is posted later.",
    )


class Refused(Exception):
    """Why nothing was started, for the model."""


# --- The description ----------------------------------------------------------------------------

DESCRIPTION = """Start one of the workflows below with an input, to hand it structured work. \
Name it by its id (or its name), and give an input that fits its input schema (JSON), or text \
if it has none. If the run ends within {wait} seconds you get its output, or its error; \
otherwise you get its run id, and its result is posted later.

Workflows you may start:
{workflows}"""


def _listing(store: Session, ids: list[int]) -> str:
    """The allowed workflows, each a line or two: id, name, description and input schema."""
    found = {w.id: w for w in store.scalars(select(Workflow).where(Workflow.id.in_(ids or [0])))}
    lines = []
    for workflow_id in dict.fromkeys(ids):
        workflow = found.get(workflow_id)
        if workflow is None:
            continue
        line = f"- {workflow.id}: {workflow.name}"
        if workflow.archived_at is not None:
            line += " (archived: it can't be started now)"
        if workflow.description.strip():
            line += f". {workflow.description.strip()}"
        version = workflow.current_version
        schema = None if version is None else version.input_schema
        line += (
            f"\n  Input schema: {json.dumps(schema, separators=(',', ':'))}"
            if schema is not None
            else "\n  Input: text"
        )
        lines.append(line)
    return "\n".join(lines) or "(none)"


# --- Starting a run -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Started:
    run_id: int
    invocation_id: int
    workflow_name: str


def _calling(store: Session, charge: usage.Charge) -> Invocation | None:
    """The calling turn's invocation, if it has one."""
    turn = store.get(Turn, charge.turn_id)
    if turn is None:
        raise Refused(NO_TURN)
    return store.get(Invocation, turn.invocation_id) if turn.invocation_id else None


def _find(store: Session, config: StartWorkflowConfig, workflow: str | int) -> Workflow:
    """The allowed workflow named by its id or name (`Refused` if it isn't one)."""
    allowed = list(dict.fromkeys(config.workflow_ids))
    given = str(workflow).strip()
    workflows = list(store.scalars(select(Workflow).where(Workflow.id.in_(allowed or [0]))))
    found = next((w for w in workflows if str(w.id) == given), None)
    if found is None:
        alike = [w for w in workflows if w.name.lower() == given.lower()]
        found = alike[0] if len(alike) == 1 else None
    if found is not None:
        return found
    if given.isdigit() and int(given) in allowed:
        raise Refused(DELETED.format(workflow=given))
    names = ", ".join(f"{w.name} ({w.id})" for w in workflows if w.archived_at is None)
    yours = YOURS.format(names=names) if names else NONE_ALLOWED
    raise Refused(NOT_ALLOWED.format(workflow=given, yours=yours))


def _check_chain(store: Session, calling: Invocation | None, workflow: Workflow) -> list[int]:
    """The chain of causes the run would continue (`Refused` if the workflow is in it already,
    or it would be too long)."""
    chain = [] if calling is None else [*calling.ancestor_ids, calling.id]
    if chain and store.scalar(
        select(Invocation.id)
        .where(Invocation.id.in_(chain), Invocation.workflow_id == workflow.id)
        .limit(1)
    ):
        raise Refused(IN_CHAIN.format(name=workflow.name))
    most = get_settings().chain_max_depth
    if len(chain) + 1 > most:
        raise Refused(TOO_DEEP.format(most=most))
    return chain


def _check_invocable(organisation_id: int, person: int | None, workflow: Workflow) -> None:
    """The workflow's invocable by, against the role of the person behind the chain."""
    if person is None:
        return
    with SessionLocal() as db:
        role = organisations.role(db, organisation_id, person)
    allowed = guards.ALLOWED[workflow.invocable_by]
    if role is None or role not in allowed:
        who = guards.WHO.get(workflow.invocable_by, "members")
        reason = f"Only {who} can invoke the workflow {workflow.name}."
        raise Refused(NOT_INVOCABLE.format(reason=reason))


def _check_rate(organisation_id: int, agent_id: int) -> None:
    try:
        ratelimits.throttle(
            "start-workflow",
            get_settings().start_workflow_rate_per_agent,
            f"{organisation_id}:{agent_id}",
        )
    except HTTPException as exc:
        raise Refused(RATE) from exc


def _detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        detail = detail.get("message", "")
    text = str(detail).strip()
    return text if text.endswith(".") else f"{text}."


def start(
    store: Session,
    organisation_id: int,
    charge: usage.Charge,
    config: StartWorkflowConfig,
    workflow: str | int,
    given: Any,
) -> tuple[_Started, Any]:
    """Start a run of the allowed workflow with the input `given`, from the charge's turn: the
    run, and its first advance's item, enqueued once the store has committed (`Refused` with
    why if it can't start; nothing is written)."""
    from aios.api import workflows as workflows_api

    if charge.agent_id is None or charge.turn_id is None:
        raise Refused(NO_TURN)
    calling = _calling(store, charge)
    target = _find(store, config, workflow)
    if target.archived_at is not None:
        raise Refused(ARCHIVED.format(name=target.name))
    _check_chain(store, calling, target)
    person = calling.requested_by if calling is not None else charge.person_id
    _check_invocable(organisation_id, person, target)
    charged_to = usage.person_of(calling) if calling is not None else charge.person_id
    if reason := guards.check_budget(store, organisation_id, charged_to):
        raise Refused(reason)
    text, value = (given, None) if isinstance(given, str) else (None, given)
    if text is None and value is None:
        raise Refused(CANT_START.format(name=target.name, reason="it was given no input."))
    try:
        run = workflows_api.new_workflow_run(
            store,
            organisation_id,
            target,
            text,
            input_json=value,
            created_by=person,
            started_by=calling,
        )
    except HTTPException as exc:
        raise Refused(CANT_START.format(name=target.name, reason=_detail(exc))) from exc
    invocation_id = store.scalar(select(Invocation.id).where(Invocation.workflow_run_id == run.id))
    # Counted only once everything else allows it.
    _check_rate(organisation_id, charge.agent_id)
    usage.record(store, charge, UsageKind.tool_call, cost=0.0, call_key=f"start_workflow:{run.id}")
    try:
        work = workflows_api.queue_workflow_run(store, run, organisation_id)
    except HTTPException as exc:
        raise Refused(CANT_START.format(name=target.name, reason=_detail(exc))) from exc
    return _Started(run.id, invocation_id, target.name), work


def _start(
    data_store_id: uuid.UUID,
    organisation_id: int,
    charge: usage.Charge,
    config: StartWorkflowConfig,
    workflow: str | int,
    given: Any,
):
    with store_session(data_store_id, "rw") as store:
        try:
            return start(store, organisation_id, charge, config, workflow, given)
        except Refused:
            store.rollback()
            raise


# --- Waiting, and handing the result back -------------------------------------------------------


def outcome(run: WorkflowRun) -> dict:
    """What the call returns for an ended run: its output, or its error."""
    if run.status == WorkflowRunStatus.succeeded:
        output = run.output_json if run.output_json is not None else run.output
        return {"run_id": run.id, "status": "succeeded", "output": output}
    return {"run_id": run.id, "status": "failed", "error": run.error or "The run failed."}


def _ended(data_store_id: uuid.UUID, run_id: int) -> dict | None:
    with store_session(data_store_id, "ro") as store:
        run = store.get(WorkflowRun, run_id)
        if run is None:
            return {"run_id": run_id, "status": "failed", "error": "The run was deleted."}
        return outcome(run) if run.status in ENDED else None


def destination(store: Session, calling: Invocation | None) -> Chat | None:
    """Where a slow run's result is handed back: the calling turn's conversation (its
    invocation's chat, or its nearest ancestor's), else the chain's trigger's delivery chat;
    None for neither (or one that's archived)."""
    if calling is None:
        return None
    chain = [calling.id, *reversed(calling.ancestor_ids)]
    rows = {i.id: i for i in store.scalars(select(Invocation).where(Invocation.id.in_(chain)))}
    ordered = [rows[i] for i in chain if i in rows]
    for column in ("chat_id", "delivery_chat_id"):
        for invocation in ordered:
            chat_id = getattr(invocation, column)
            if chat_id is None:
                continue
            chat = store.get(Chat, chat_id)
            if chat is not None and chat.archived_at is None:
                return chat
    return None


def _later(data_store_id: uuid.UUID, started: _Started) -> dict:
    """The run hasn't ended within the wait: have its result handed back where the calling turn
    is (see `destination`), and say so. Under the invocation's lock, so a run that ends just now
    is returned instead."""
    with store_session(data_store_id, "rw") as store:
        invocation = store.scalars(
            select(Invocation).where(Invocation.id == started.invocation_id).with_for_update()
        ).one_or_none()
        run = store.get(WorkflowRun, started.run_id)
        if run is not None and run.status in ENDED:
            return outcome(run)
        result = {"run_id": started.run_id, "status": "running"}
        if invocation is None or invocation.status not in (
            InvocationStatus.queued,
            InvocationStatus.briefing,
            InvocationStatus.running,
        ):
            return result | {"message": RUN_PAGE}
        calling_id = invocation.ancestor_ids[-1] if invocation.ancestor_ids else None
        calling = store.get(Invocation, calling_id) if calling_id is not None else None
        chat = destination(store, calling)
        if chat is None:
            return result | {"message": RUN_PAGE}
        invocation.delivery_chat_id = chat.id
        store.commit()
        return result | {"message": WILL_BE_POSTED.format(chat=chat.name)}


async def _wait(data_store_id: uuid.UUID, started: _Started, wait: float) -> dict:
    deadline = time.monotonic() + wait
    poll = get_settings().start_workflow_poll_seconds
    while True:
        ended = await run_in_threadpool(_ended, data_store_id, started.run_id)
        if ended is not None:
            return ended
        left = deadline - time.monotonic()
        if left <= 0:
            return await run_in_threadpool(_later, data_store_id, started)
        await asyncio.sleep(min(poll, left))


async def call(
    data_store_id: uuid.UUID,
    organisation_id: int,
    charge: usage.Charge,
    config: StartWorkflowConfig,
    workflow: str | int,
    given: Any,
) -> dict:
    from aios import run_service
    from aios.run_service import executor

    try:
        started, work = await in_thread(
            data_store_id, _start, organisation_id, charge, config, workflow, given
        )
    except Refused as exc:
        return {"error": str(exc)}
    # Here rather than on a worker thread: the publish is quick (its timeouts are a second),
    # and a process draining in place (tests, `executor.drain_once`) is nudged instead.
    run_service.wake(work.lane)
    executor.nudge()
    return await _wait(data_store_id, started, config.wait_seconds)


def build_in(config: StartWorkflowConfig, org: ToolOrganisation) -> list[AgentTool]:
    description = DESCRIPTION.format(
        wait=config.wait_seconds, workflows=_listing(org.store, config.workflow_ids)
    )
    organisation_id = org.organisation_id

    async def start_workflow(
        workflow: Annotated[int | str, "The workflow to start: its id, or its name."],
        input: Annotated[
            Any, "The run's input: JSON that fits the workflow's input schema, or text."
        ],
        runtime: ToolRuntime[ChatDeps],
    ) -> str:
        charge = usage.current()
        if runtime.context.run_id is None or charge is None:
            return json.dumps({"error": NO_TURN})
        return await in_store(runtime, call, organisation_id, charge, config, workflow, input)

    return [StructuredTool.from_function(coroutine=start_workflow, description=description)]


def build(config: StartWorkflowConfig) -> list[AgentTool]:
    """Without the organisation (never at run time): a description that can't list them."""

    async def start_workflow(
        workflow: Annotated[int | str, "The workflow to start: its id, or its name."],
        input: Annotated[Any, "The run's input."],
        runtime: ToolRuntime[ChatDeps],
    ) -> str:
        return json.dumps({"error": NO_TURN})

    return [
        StructuredTool.from_function(
            coroutine=start_workflow,
            description=DESCRIPTION.format(wait=config.wait_seconds, workflows="(unknown)"),
        )
    ]


register(
    ToolSpec(
        key="start_workflow",
        name="Start workflow",
        description="Start the workflows it's allowed to with structured input: their output"
        " comes straight back if the run ends within the wait time, else it's posted later.",
        config_model=StartWorkflowConfig,
        build=build,
        build_in=build_in,
    )
)
