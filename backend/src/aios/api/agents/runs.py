"""An agent's runs: listing, starting, reading and deleting them."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from aios import checkpoints, stored, ui_stream
from aios.api.agents.common import get_org_agent, lock_live_agent
from aios.api.provenance import UserOut, started_by
from aios.api.schema_changes import SchemaChangeOut, run_requests, waiting_on
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import history, schema_changes
from aios.datastores.models import Agent, AgentVersion
from aios.db import DB, DbId
from aios.models import (
    AgentRun,
    AgentRunStatus,
    Chat,
    ChatInvocation,
    Organisation,
    SchemaChangeRequest,
    WorkflowRunStep,
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


def get_org_run(db: Session, org: Organisation, agent_id: int, run_id: int) -> AgentRun:
    """A run of the organisation's agent (archived or not); a 404 otherwise, as if it didn't
    exist. Every member reads, continues and deletes every run of the organisation's, whoever
    started it and however (by hand, from a chat, by a schedule)."""
    run = get_live_run(db, agent_id, run_id)
    if run.organisation_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def get_live_run(db: Session, agent_id: int, run_id: int) -> AgentRun:
    """A run of the agent (by its id in the run's organisation's store), whoever's it is; a 404
    otherwise."""
    run = db.get(AgentRun, run_id)
    if run is None or run.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def run_summary_query():
    return (
        select(
            AgentRun.id,
            AgentRun.agent_id,
            AgentRun.agent_version_id,
            AgentRun.title,
            AgentRun.message_count,
            AgentRun.trigger_id,
            AgentRun.status,
            AgentRun.started_at,
            AgentRun.finished_at,
            AgentRun.tool_call_count,
            AgentRun.input_tokens,
            AgentRun.output_tokens,
            Chat.id.label("chat_id"),
            Chat.name.label("chat_name"),
            # As the user, by `run_summary` and `list_runs` (`provenance.started_by`).
            AgentRun.created_by.label("created_by_id"),
            AgentRun.created_at,
            AgentRun.updated_at,
        )
        .outerjoin(ChatInvocation, ChatInvocation.agent_run_id == AgentRun.id)
        .outerjoin(Chat, Chat.id == ChatInvocation.chat_id)
    )


def run_summaries(db: Session, organisation_id: int, rows: Sequence[Row]) -> list[RunSummary]:
    """Rows of `run_summary_query` for the organisation's runs as summaries: with the numbers
    of the versions they pinned, from its store, and who started each."""
    numbers = stored.version_numbers(
        organisation_id, AgentVersion, [row.agent_version_id for row in rows]
    )
    return started_by(
        db,
        organisation_id,
        [{**row._mapping, "agent_version": numbers.get(row.agent_version_id)} for row in rows],
        RunSummary,
    )


def run_summary(db: Session, run_id: int) -> RunSummary:
    row = db.execute(run_summary_query().where(AgentRun.id == run_id)).one()
    organisation_id = db.scalar(select(AgentRun.organisation_id).where(AgentRun.id == run_id))
    return run_summaries(db, organisation_id, [row])[0]


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
    rows = db.execute(
        run_summary_query()
        .where(AgentRun.organisation_id == org.id, AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return run_summaries(db, org.id, rows)


def new_agent_run(
    db: Session,
    store: Session,
    organisation_id: int,
    agent: Agent,
    *,
    created_by: int | None,
    trigger_id: int | None = None,
) -> AgentRun:
    """Add (and flush, not commit) a run of the agent's current version (the agent in the
    organisation `organisation_id`'s store, `store`), started by the member `created_by`: who
    asked for it by hand or in a chat. It runs for the agent's organisation, not for them
    (`load_chat_context`).

    Raises `HTTPException` if it can't run: 409 if the agent is archived ("The agent <name> is
    archived.") or its version can't be built. The caller commits `db`, then ends `store`'s
    transaction; the share lock taken here, held until then, keeps a concurrent archive from
    missing the run. Also how a schedule starts one (`aios.scheduler`), passing its
    `trigger_id` and no member.
    """
    lock_live_agent(store, agent, read=True)
    # Checked now, so a version that can't be run doesn't leave behind a run that can't be used.
    build_version_tools(agent.current_version, ToolOrganisation(organisation_id, store))
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(
        organisation_id=organisation_id,
        agent_id=agent.id,
        agent_version_id=agent.current_version_id,
        trigger_id=trigger_id,
        created_by=created_by,
    )
    db.add(run)
    db.flush()
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
    run = new_agent_run(db, store, org.id, agent, created_by=user.id)
    db.commit()
    store.commit()
    return run_summary(db, run.id)


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
    run = get_org_run(db, org, agent_id, run_id)
    summary = run_summary(db, run.id)
    messages = checkpoints.history(run.id)
    return RunOut(
        **summary.model_dump(),
        agent_name=get_org_agent(store, run.agent_id).name,
        ui_messages=ui_stream.to_ui_messages(messages),
        messages=checkpoints.messages_out(messages),
        instructions=store.get_one(AgentVersion, run.agent_version_id).instructions,
        schema_change_requests=run_requests(db, run.id, user.id),
        waiting=waiting_on(db, run.id),
    )


@router.delete(
    "/agents/{agent_id}/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_run(
    agent_id: DbId, run_id: DbId, org: CurrentOrganisation, db: DB, background: BackgroundTasks
) -> None:
    run = get_org_run(db, org, agent_id, run_id)
    sandboxes = run_sandboxes(db, [run.id])
    cancel_chat_requests(db, [run.id])
    # Its exported copy goes too (found through the run, so before it's deleted).
    history.enqueue_runs(db, AgentRun.id == run.id, deleting=True)
    db.delete(run)
    db.commit()
    # A turn still underway stops now, rather than carrying on for a run that's gone.
    checkpoints.stop_turns(db, [run_id])
    background.add_task(checkpoints.delete_threads, [run_id])
    if sandboxes:
        background.add_task(stop_sandboxes, sandboxes)


def cancel_chat_requests(db: Session, run_ids: list[int]) -> None:
    """Before these agent runs are deleted (their requests keep no link to them): cancel the
    pending requests their chats wait on, so none can be approved with nothing left to resume.
    A workflow node's run is left alone: its workflow run still waits on the request and resumes
    on its decision (`aios.workflows.workflow_run_waiting_on`), and ending or deleting that run
    closes it out (`aios.workflows.close_requests`). In the caller's transaction.

    The runs are locked first: a turn recording a request meanwhile holds a key share on its
    run until it commits, so this waits and then sees it; one that starts later fails to
    record it once the run has gone."""
    db.execute(select(AgentRun.id).where(AgentRun.id.in_(run_ids)).with_for_update())
    schema_changes.cancel(
        db,
        SchemaChangeRequest.requested_by_run.in_(run_ids),
        SchemaChangeRequest.requested_by_run.not_in(
            select(WorkflowRunStep.agent_run_id).where(WorkflowRunStep.agent_run_id.in_(run_ids))
        ),
    )
