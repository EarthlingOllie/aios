"""An agent's runs: listing, starting, reading and deleting them."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from aios import checkpoints, invocations, ui_stream
from aios.api.agents.common import get_org_agent, lock_live_agent
from aios.api.provenance import UserOut, started_by
from aios.api.schema_changes import SchemaChangeOut, run_requests, waiting_on
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import history, schema_changes, store_id_of
from aios.datastores.models import Agent, AgentRun, AgentVersion, WorkflowRunStep
from aios.db import DB, DbId
from aios.models import (
    AgentRunStatus,
    Chat,
    ChatInvocation,
    SchemaChangeRequest,
)
from aios.permissions import Action, allow
from aios.runner import version_tools
from aios.sandbox.sessions import (
    run_sandboxes,
    stop_sandboxes,
)
from aios.tools import (
    AgentTool,
    ToolError,
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
    # and tokens over all its turns. All null for a run from before these were kept.
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
    return select(
        AgentRun.id,
        AgentRun.agent_id,
        AgentRun.agent_version_id,
        AgentVersion.version.label("agent_version"),
        AgentRun.title,
        AgentRun.message_count,
        AgentRun.trigger_id,
        AgentRun.status,
        AgentRun.started_at,
        AgentRun.finished_at,
        AgentRun.tool_call_count,
        AgentRun.input_tokens,
        AgentRun.output_tokens,
        # As the user, by `run_summaries` (`provenance.started_by`).
        AgentRun.created_by.label("created_by_id"),
        AgentRun.created_at,
        AgentRun.updated_at,
    ).join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)


def run_chats(
    db: Session, organisation_id: int, invocation_run, run_ids: Sequence[int]
) -> dict[int, tuple[int, str]]:
    """The chats whose mentions started these runs of the organisation's (by the invocation's
    column naming them, `invocation_run`), while the chat exists: (id, name) by run id."""
    if not run_ids:
        return {}
    rows = db.execute(
        select(invocation_run, Chat.id, Chat.name)
        .join(Chat, Chat.id == ChatInvocation.chat_id)
        .where(Chat.organisation_id == organisation_id, invocation_run.in_(list(run_ids)))
    ).all()
    return {run_id: (chat_id, name) for run_id, chat_id, name in rows}


def run_summaries(db: Session, organisation_id: int, rows: Sequence[Row]) -> list[RunSummary]:
    """Rows of `run_summary_query` for the organisation's runs as summaries: with the chats
    that started them and who started each, from the platform."""
    chats = run_chats(db, organisation_id, ChatInvocation.agent_run_id, [r.id for r in rows])
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
    return run_summaries(db, organisation_id, [row])[0]


def build_version_tools(
    version: AgentVersion, refusal: str = "This agent version can't be run"
) -> list[AgentTool]:
    """The version's tools, built from their stored settings; a 409 if one can't be."""
    try:
        return version_tools(version)
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
    return run_summaries(db, org.id, rows)


def new_agent_run(
    store: Session,
    agent: Agent,
    *,
    created_by: int | None,
    trigger_id: int | None = None,
) -> AgentRun:
    """Add (and flush, not commit) a run of the agent's current version to the organisation's
    store (`store`, where the agent is), started by the member `created_by`: who asked for it
    by hand or in a chat. It runs for the agent's organisation, not for them
    (`load_chat_context`).

    Raises `HTTPException` if it can't run: 409 if the agent is archived ("The agent <name> is
    archived.") or its version can't be built. The caller commits `store`; the share lock taken
    here, held until then, keeps a concurrent archive from missing the run. Also how a schedule
    starts one (`aios.scheduler`), passing its `trigger_id` and no member.
    """
    lock_live_agent(store, agent, read=True)
    # Checked now, so a version that can't be run doesn't leave behind a run that can't be used.
    build_version_tools(agent.current_version)
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(
        agent_id=agent.id,
        agent_version_id=agent.current_version_id,
        trigger_id=trigger_id,
        created_by=created_by,
    )
    store.add(run)
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
    agent = get_org_agent(store, agent_id)
    run = new_agent_run(store, agent, created_by=user.id)
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
        schema_change_requests=run_requests(db, store_id, run, user.id),
        waiting=waiting_on(db, store_id, run.id),
    )


@router.delete(
    "/agents/{agent_id}/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_run(
    agent_id: DbId, run_id: DbId, db: DB, store: CurrentStore, background: BackgroundTasks
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
    checkpoints.delete_threads(store, [run.id])
    store.delete(run)  # and its sandbox session
    store.commit()
    # Then what the platform says of it: the store is the run's source of truth. A request its
    # turn is recording meanwhile is waited for (`schema_changes.lock_run`), and then seen.
    schema_changes.lock_run(db, store_id, run_id)
    if in_workflow is None:
        cancel_chat_requests(db, store_id, [run_id])
    # Its exported copy goes too, and the copy of the workflow run whose step held it.
    history.enqueue_runs(db, store_id, [run_id], deleting=True)
    if in_workflow is not None:
        history.enqueue_workflow_runs(db, store_id, [in_workflow])
    invocations.forget_runs(db, store_id, agent_run_ids=[run_id])
    db.commit()
    # A turn still underway stops now, rather than carrying on for a run that's gone.
    checkpoints.stop_turns(store, store_id, [run_id])
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


def cancel_chat_requests(db: Session, data_store_id: uuid.UUID, run_ids: list[int]) -> None:
    """Once these agent runs of the store are deleted (not workflow nodes' runs: their workflow
    runs still wait on their requests, and resume on their decisions,
    `aios.workflows.workflow_run_waiting_on`; ending or deleting the workflow run closes them
    out, `aios.workflows.close_requests`): cancel the pending requests their chats waited on, so
    none can be approved with nothing left to resume. In the caller's transaction, before
    `invocations.forget_runs` clears the requests' runs.

    Take the runs' locks first (`schema_changes.lock_run`): a request being recorded for one
    is waited for, and then seen, and none can be recorded once it's gone."""
    schema_changes.cancel(
        db,
        SchemaChangeRequest.data_store_id == data_store_id,
        SchemaChangeRequest.requested_by_run.in_(run_ids),
    )
