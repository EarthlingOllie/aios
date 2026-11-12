"""An agent's runs: listing, starting, reading and deleting them."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from aios import checkpoints, guards, invocations, turns, ui_stream
from aios.api.agents.common import get_org_agent, lock_live_agent
from aios.api.approvals import SchemaChangeOut, run_requests, waiting_on
from aios.api.provenance import UserOut, started_by
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import schema_changes, store_id_of
from aios.datastores.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Chat,
    Event,
    Invocation,
    SchemaChangeRequest,
    Turn,
    WorkflowRunStep,
)
from aios.db import DB, DbId
from aios.models import AgentRunStatus, InvocationCause, InvocationStatus, TriggerTargetKind
from aios.permissions import Action, allow
from aios.runner import version_tools
from aios.sandbox.sessions import (
    run_sandboxes,
    stop_sandboxes,
)
from aios.tools import (
    AgentTool,
    ToolError,
    ToolOrganisation,
)

router = APIRouter(prefix="/api")


class RunSummary(BaseModel):
    id: int
    agent_id: int
    agent_version_id: int
    agent_version: int
    title: str | None
    message_count: int
    # The trigger that started the run (e.g. a schedule), if one did and it still exists.
    trigger_id: int | None
    # How its latest turn is going or went, and when it began and ended; the model's tool calls
    # and tokens over all its turns (`aios.turns`). All null for a run with no turns yet.
    status: AgentRunStatus | None
    started_at: datetime | None
    finished_at: datetime | None
    tool_call_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    # The chat a mention in it started it from, while the chat exists.
    chat_id: int | None
    chat_name: str | None
    # The member who started it: by hand, by mentioning the agent in a chat, or by starting the
    # workflow run it's a node's conversation in. Null for a schedule's run (`trigger_id` says
    # which, while it exists), and once that user is deleted.
    created_by: UserOut | None
    created_at: datetime
    updated_at: datetime


class InvocationRecordOut(BaseModel):
    """Why a run ran (`aios.invocations`)."""

    id: int
    cause: InvocationCause
    status: InvocationStatus
    # Who or what caused it: the member, the schedule (while it exists), the parent invocation
    # (a workflow agent node's: its workflow run's).
    requested_by: int | None
    trigger_id: int | None
    parent_id: int | None
    # Its ancestors' ids, the root first, and how many there are.
    ancestor_ids: list[int]
    depth: int
    chat_id: int | None
    message_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class TurnOut(BaseModel):
    """One turn of the run (`aios.turns`)."""

    id: int
    invocation_id: int | None
    # The agent version it ran on, by id and number.
    agent_version_id: int
    agent_version: int
    status: AgentRunStatus
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


def run_invocation(store: Session, run_id: int) -> InvocationRecordOut | None:
    """The invocation of the store's agent run."""
    invocation = store.scalars(
        select(Invocation).where(Invocation.agent_run_id == run_id).order_by(Invocation.id)
    ).first()
    return None if invocation is None else InvocationRecordOut.model_validate(invocation)


def run_turns(store: Session, run_id: int) -> list[TurnOut]:
    """The store's agent run's turns, oldest first."""
    rows = store.execute(
        select(Turn, AgentVersion.version)
        .join(AgentVersion, AgentVersion.id == Turn.agent_version_id)
        .where(Turn.agent_run_id == run_id)
        .order_by(Turn.id)
    ).all()
    return [
        TurnOut(
            id=turn.id,
            invocation_id=turn.invocation_id,
            agent_version_id=turn.agent_version_id,
            agent_version=version,
            status=turn.status,
            error=turn.error,
            started_at=turn.started_at,
            finished_at=turn.finished_at,
            model_calls=turn.model_calls,
            tool_calls=turn.tool_calls,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
        )
        for turn, version in rows
    ]


class RunOut(RunSummary):
    # The agent's name, so the page needn't read the agent as well.
    agent_name: str
    # AI SDK `UIMessage`s, for rendering the conversation with the same components as a live chat.
    ui_messages: list[dict[str, Any]]
    # The raw LangChain history: messages with their tool calls, results and usage.
    messages: list[dict[str, Any]]
    # The pinned version's instructions: the system prompt of every model call in the run.
    instructions: str
    # The schema change requests the run made (oldest first), each at its tool call.
    schema_change_requests: list[SchemaChangeOut]
    # Whether it waits on one of them, and takes no new messages until it's resumed.
    waiting: bool
    # Why it ran; and its turns, oldest first.
    invocation: InvocationRecordOut | None
    turns: list[TurnOut]


def get_org_run(store: Session, agent_id: int, run_id: int) -> AgentRun:
    """A run of the organisation's agent (archived or not), in its store (`store`); a 404
    otherwise, as if it didn't exist. Ids are the store's own, so another organisation's run is
    never reached. Every member reads, continues and deletes every run of the organisation's,
    whoever started it and however (by hand, from a chat, by a schedule)."""
    run = store.get(AgentRun, run_id)
    if run is None or run.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def run_summary_query():
    """The store's runs, as summaries' columns (`run_summaries` adds the rest)."""
    return turns.run_columns(
        select(
            AgentRun.id,
            AgentRun.agent_id,
            AgentRun.agent_version_id,
            AgentVersion.version.label("agent_version"),
            AgentRun.title,
            AgentRun.message_count,
            AgentRun.trigger_id,
            # As the user, by `run_summaries` (`provenance.started_by`).
            AgentRun.created_by.label("created_by_id"),
            AgentRun.created_at,
            AgentRun.updated_at,
        ).join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)
    )


def run_chats(store: Session, invocation_run, run_ids: Sequence[int]) -> dict[int, tuple[int, str]]:
    """The chats whose mentions started these runs of the store's (by the invocation's column
    naming them, `invocation_run`), while the chat exists: (id, name) by run id."""
    if not run_ids:
        return {}
    rows = store.execute(
        select(invocation_run, Chat.id, Chat.name)
        .join(Chat, Chat.id == Invocation.chat_id)
        .where(invocation_run.in_(list(run_ids)))
    ).all()
    return {run_id: (chat_id, name) for run_id, chat_id, name in rows}


def run_summaries(
    db: Session, organisation_id: int, store: Session, rows: Sequence[Row]
) -> list[RunSummary]:
    """Rows of `run_summary_query` for the organisation's runs (in its store, `store`) as
    summaries: with the chats that started them, and who started each."""
    chats = run_chats(store, Invocation.agent_run_id, [r.id for r in rows])
    return started_by(
        db,
        organisation_id,
        [
            {
                **row._mapping,
                "chat_id": chats.get(row.id, (None, None))[0],
                "chat_name": chats.get(row.id, (None, None))[1],
            }
            for row in rows
        ],
        RunSummary,
    )


def run_summary(db: Session, organisation_id: int, store: Session, run_id: int) -> RunSummary:
    row = store.execute(run_summary_query().where(AgentRun.id == run_id)).one()
    return run_summaries(db, organisation_id, store, [row])[0]


def build_version_tools(
    version: AgentVersion, org: ToolOrganisation, refusal: str = "This agent version can't be run"
) -> list[AgentTool]:
    """The version's tools, built from their stored settings to run in `org`; a 409 if one
    can't be."""
    try:
        return version_tools(version, org)
    except ToolError as exc:
        # E.g. the tool has since left the library, or its settings model now rejects a value.
        raise HTTPException(status.HTTP_409_CONFLICT, f"{refusal}: {exc}") from exc


@router.get(
    "/agents/{agent_id}/runs", response_model=list[RunSummary], dependencies=[allow(Action.read)]
)
def list_runs(agent_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    agent = get_org_agent(store, agent_id)
    rows = store.execute(
        run_summary_query()
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return run_summaries(db, org.id, store, rows)


def new_agent_run(
    store: Session,
    organisation_id: int,
    agent: Agent,
    *,
    created_by: int | None,
    trigger_id: int | None = None,
    by_mention: bool = False,
    event: Event | None = None,
) -> AgentRun:
    """Add (and flush, not commit) a run of the agent's current version to the store of the
    organisation `organisation_id` (`store`, where the agent is), started by the member
    `created_by`: who asked for it by hand or in a chat. It runs for the agent's organisation,
    not for them (`load_chat_context`), and its tools are checked against it (`ToolSpec.check`).

    Raises `HTTPException` if it can't run: 409 if the agent is archived ("The agent <name> is
    archived.") or its version can't be built. The caller commits `store`; the share lock taken
    here, held until then, keeps a concurrent archive from missing the run. Also how a schedule
    starts one (`aios.scheduler`), passing its `trigger_id` and no member.

    Its invocation is made with it (`invocations.for_run`): a schedule's, an event trigger's
    (with the `event` it fired on), or one started by hand by `created_by`. A mention's run
    (`by_mention`) has its mention's, which the caller links.
    """
    lock_live_agent(store, agent, read=True)
    # Checked now, so a version that can't be run doesn't leave behind a run that can't be used.
    build_version_tools(agent.current_version, ToolOrganisation(organisation_id, store))
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(
        agent_id=agent.id,
        agent_version_id=agent.current_version_id,
        trigger_id=trigger_id,
        created_by=created_by,
    )
    store.add(run)
    store.flush()
    if not by_mention:
        invocation = invocations.for_run(
            store,
            TriggerTargetKind.agent,
            agent.id,
            requested_by=created_by,
            trigger_id=trigger_id,
            event=event,
        )
        invocation.agent_run_id = run.id
        store.flush()
    return run


@router.post(
    "/agents/{agent_id}/runs",
    response_model=RunSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.post)],
)
def create_run(
    agent_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Start a run of the agent by hand: a 409 with why at the organisation's budget or the
    person's allowance (`aios.budgets`)."""
    agent = get_org_agent(store, agent_id)
    guards.refuse_at_budget(store, org.id, user.id)
    run = new_agent_run(store, org.id, agent, created_by=user.id)
    store.commit()
    return run_summary(db, org.id, store, run.id)


@router.get(
    "/agents/{agent_id}/runs/{run_id}", response_model=RunOut, dependencies=[allow(Action.read)]
)
def get_run(
    agent_id: DbId,
    run_id: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    run = get_org_run(store, agent_id, run_id)
    store_id = store_id_of(store)
    summary = run_summary(db, org.id, store, run.id)
    messages = checkpoints.history(store_id, run.id)
    return RunOut(
        **summary.model_dump(),
        agent_name=get_org_agent(store, run.agent_id).name,
        ui_messages=ui_stream.to_ui_messages(messages),
        messages=checkpoints.messages_out(messages),
        instructions=store.get_one(AgentVersion, run.agent_version_id).instructions,
        schema_change_requests=run_requests(db, store, run, user.id),
        waiting=waiting_on(store, run.id),
        invocation=run_invocation(store, run.id),
        turns=run_turns(store, run.id),
    )


@router.delete(
    "/agents/{agent_id}/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_run(
    agent_id: DbId, run_id: DbId, store: CurrentStore, background: BackgroundTasks
) -> None:
    run = get_org_run(store, agent_id, run_id)
    store_id = store_id_of(store)
    # Locked first: a write of a turn underway holds a share lock on it until it commits (its
    # fence, `checkpoints.FencedSaver`), so the thread is deleted after it, and no later write
    # gets through.
    store.execute(select(AgentRun.id).where(AgentRun.id == run.id).with_for_update())
    sandboxes = run_sandboxes(store, [run.id])
    # A workflow node's run: its workflow run still waits on the run's requests.
    in_workflow = store.scalar(
        select(WorkflowRunStep.run_id).where(WorkflowRunStep.agent_run_id == run.id).limit(1)
    )
    # A request its turn is recording meanwhile has been waited for by the lock above
    # (`schema_changes.lock_run`), and is seen here.
    cancelled = [] if in_workflow is not None else cancel_chat_requests(store, [run_id])
    invocations.fail_deleted_agent_run(store, run.id)
    checkpoints.delete_threads(store, [run.id])
    # And its sandbox session; what names it (its requests, its invocation, its schedule)
    # stops naming it.
    store.delete(run)
    store.commit()
    # Then what the platform says of it: the store is the run's source of truth.
    schema_changes.clear_notifications_of(cancelled)
    # A turn still underway stops now, rather than carrying on for a run that's gone.
    checkpoints.stop_turns(store, store_id, [run_id])
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


def cancel_chat_requests(store: Session, run_ids: list[int]) -> list[uuid.UUID]:
    """As these agent runs of the store are deleted (not workflow nodes' runs: their workflow
    runs still wait on their requests, and resume on their decisions,
    `aios.workflows.workflow_run_waiting_on`; ending or deleting the workflow run closes them
    out, `aios.workflows.close_requests`): cancel the pending requests their chats waited on, so
    none can be approved with nothing left to resume. In the caller's transaction, before the
    runs' deletion clears the requests' runs; returns their ids, whose notifications to clear
    once it commits.

    Take the runs' locks first (`schema_changes.lock_run`): a request being recorded for one
    is waited for, and then seen, and none can be recorded once it's gone."""
    return schema_changes.cancel(store, SchemaChangeRequest.requested_by_run.in_(run_ids))
