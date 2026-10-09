from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import AgentRunResult, capture_run_messages
from pydantic_ai.messages import (
    CompactionPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from pydantic_ai.ui.vercel_ai.response_types import BaseChunk
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.api.skills import lock_live_skills, unique_name
from aios.auth import CurrentUser
from aios.db import DB, SessionLocal
from aios.models import Agent, AgentRun, AgentVersion, Skill, SkillVersion, agent_version_skills
from aios.runner import runner
from aios.versioning import save_agent_version

router = APIRouter(prefix="/api")

SDK_VERSION = 7


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    base_prompt: str = ""
    # Specific skill versions, in order. The UI keeps already-attached skills on their pinned
    # version and adds new ones at their latest; updating a skill is an explicit change.
    skill_version_ids: list[int] = []
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class AgentSkillOut(BaseModel):
    skill_id: int
    skill_version_id: int
    name: str
    description: str
    version: int
    # The skill's current version, or None if the skill has since been deleted.
    latest_version: int | None
    latest_version_id: int | None


class AgentVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    skill_count: int
    run_count: int


class AgentVersionOut(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    base_prompt: str
    model: str | None
    instructions: str
    skills: list[AgentSkillOut]


class AgentSummary(BaseModel):
    id: int
    name: str
    description: str
    version: int
    skill_count: int
    run_count: int
    updated_at: datetime


class AgentOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    current_version: AgentVersionOut


class RunSummary(BaseModel):
    id: int
    agent_id: int
    agent_version_id: int
    agent_version: int
    title: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime


class RunOut(RunSummary):
    # AI SDK `UIMessage`s, for rendering the conversation with the same components as a live chat.
    ui_messages: list[dict[str, Any]]
    # The raw Pydantic AI history: requests/responses, instructions, tool calls and usage.
    messages: list[dict[str, Any]]


def get_owned_agent(db: Session, user_id: int, agent_id: int) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return agent


def get_agent_version(db: Session, agent: Agent, number: int) -> AgentVersion:
    version = db.scalar(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id, AgentVersion.version == number
        )
    )
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return version


def get_owned_run(db: Session, user_id: int, agent_id: int, run_id: int) -> AgentRun:
    agent = get_owned_agent(db, user_id, agent_id)
    run = db.get(AgentRun, run_id)
    if run is None or run.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def resolve_skill_versions(db: Session, user_id: int, ids: list[int]) -> list[SkillVersion]:
    """The requested versions of the user's live skills, in request order."""
    found = {
        v.id: v
        for v in db.scalars(
            select(SkillVersion)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(SkillVersion.id.in_(ids), Skill.owner_id == user_id, Skill.deleted_at.is_(None))
        )
    }
    versions = [found[i] for i in ids if i in found]
    if len(versions) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown skill version")
    if len({v.skill_id for v in versions}) != len(versions):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Each skill can be attached only once"
        )
    # Lock the skills until commit and re-check they're live, so a concurrent delete either
    # waits for this save (and then sees the usage) or wins and we reject the attach.
    skill_ids = {v.skill_id for v in versions}
    if lock_live_skills(db, skill_ids) != skill_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown skill version")
    return versions


def version_out(version: AgentVersion) -> AgentVersionOut:
    skills = []
    for sv in version.skills:
        skill = sv.skill
        latest = skill.current_version if skill.deleted_at is None else None
        skills.append(
            AgentSkillOut(
                skill_id=skill.id,
                skill_version_id=sv.id,
                name=skill.name,
                description=sv.description,
                version=sv.version,
                latest_version=latest.version if latest else None,
                latest_version_id=latest.id if latest else None,
            )
        )
    return AgentVersionOut(
        id=version.id,
        version=version.version,
        note=version.note,
        created_at=version.created_at,
        base_prompt=version.base_prompt,
        model=version.model,
        instructions=version.instructions,
        skills=skills,
    )


def agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        current_version=version_out(agent.current_version),
    )


def skill_count(version_id) -> Any:
    return (
        select(func.count())
        .select_from(agent_version_skills)
        .where(agent_version_skills.c.agent_version_id == version_id)
        .scalar_subquery()
    )


# --- Agents -------------------------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(user: CurrentUser, db: DB):
    run_count = (
        select(func.count(AgentRun.id)).where(AgentRun.agent_id == Agent.id).scalar_subquery()
    )
    rows = db.execute(
        select(Agent, AgentVersion.version, skill_count(AgentVersion.id), run_count)
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .where(Agent.owner_id == user.id)
        .order_by(Agent.name)
    ).all()
    return [
        AgentSummary(
            id=a.id,
            name=a.name,
            description=a.description,
            version=version,
            skill_count=skills,
            run_count=runs,
            updated_at=a.updated_at,
        )
        for a, version, skills, runs in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: CurrentUser, db: DB):
    skills = resolve_skill_versions(db, user.id, body.skill_version_ids)
    agent = Agent(owner_id=user.id, name=body.name, description=body.description)
    db.add(agent)
    with unique_name(db, "an agent"):
        save_agent_version(
            db,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            note=body.note,
            user_id=user.id,
        )
        db.commit()
    return agent_out(agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: int, user: CurrentUser, db: DB):
    return agent_out(get_owned_agent(db, user.id, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: int, body: AgentIn, user: CurrentUser, db: DB):
    """Rename/describe the agent; a changed prompt or skill set creates a new version."""
    agent = get_owned_agent(db, user.id, agent_id)
    skills = resolve_skill_versions(db, user.id, body.skill_version_ids)
    agent.name = body.name
    agent.description = body.description
    with unique_name(db, "an agent"):
        save_agent_version(
            db,
            agent,
            base_prompt=body.base_prompt,
            skills=skills,
            note=body.note,
            user_id=user.id,
            model=agent.current_version.model,
        )
        db.commit()
    return agent_out(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: int, user: CurrentUser, db: DB) -> None:
    # Versions and runs belong to the agent and go with it (ON DELETE CASCADE).
    agent = get_owned_agent(db, user.id, agent_id)
    agent.current_version_id = None  # break the agents <-> agent_versions cycle first
    db.flush()
    db.delete(agent)
    db.commit()


@router.get("/agents/{agent_id}/versions", response_model=list[AgentVersionSummary])
def list_agent_versions(agent_id: int, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    run_count = (
        select(func.count(AgentRun.id))
        .where(AgentRun.agent_version_id == AgentVersion.id)
        .scalar_subquery()
    )
    rows = db.execute(
        select(AgentVersion, skill_count(AgentVersion.id), run_count)
        .where(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.version.desc())
    ).all()
    return [
        AgentVersionSummary(
            id=v.id,
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            skill_count=skills,
            run_count=runs,
        )
        for v, skills, runs in rows
    ]


@router.get("/agents/{agent_id}/versions/{number}", response_model=AgentVersionOut)
def get_agent_version_detail(agent_id: int, number: int, user: CurrentUser, db: DB):
    return version_out(get_agent_version(db, get_owned_agent(db, user.id, agent_id), number))


@router.post("/agents/{agent_id}/versions/{number}/restore", response_model=AgentOut)
def restore_agent_version(agent_id: int, number: int, user: CurrentUser, db: DB):
    """Make an old version's config current again, as a new version (numbers only go up)."""
    agent = get_owned_agent(db, user.id, agent_id)
    old = get_agent_version(db, agent, number)
    skill_ids = {sv.skill_id for sv in old.skills}
    if lock_live_skills(db, skill_ids) != skill_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That version uses a skill that has since been deleted."
        )
    save_agent_version(
        db,
        agent,
        base_prompt=old.base_prompt,
        skills=list(old.skills),
        model=old.model,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    db.commit()
    return agent_out(agent)


# --- Runs ---------------------------------------------------------------------------------------


def run_summary_query():
    return select(
        AgentRun.id,
        AgentRun.agent_id,
        AgentRun.agent_version_id,
        AgentVersion.version.label("agent_version"),
        AgentRun.title,
        func.jsonb_array_length(AgentRun.messages).label("message_count"),
        AgentRun.created_at,
        AgentRun.updated_at,
    ).join(AgentVersion, AgentVersion.id == AgentRun.agent_version_id)


@router.get("/agents/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: int, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    rows = db.execute(
        run_summary_query()
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return [RunSummary.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/agents/{agent_id}/runs", response_model=RunSummary, status_code=status.HTTP_201_CREATED
)
def create_run(agent_id: int, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    # Pin the version: later edits to the agent don't change this conversation.
    run = AgentRun(agent=agent, agent_version_id=agent.current_version_id, messages=[])
    db.add(run)
    db.commit()
    row = db.execute(run_summary_query().where(AgentRun.id == run.id)).one()
    return RunSummary.model_validate(row, from_attributes=True)


@router.get("/agents/{agent_id}/runs/{run_id}", response_model=RunOut)
def get_run(agent_id: int, run_id: int, user: CurrentUser, db: DB):
    run = get_owned_run(db, user.id, agent_id, run_id)
    row = db.execute(run_summary_query().where(AgentRun.id == run.id)).one()
    history = ModelMessagesTypeAdapter.validate_python(run.messages)
    ui_messages = VercelAIAdapter.dump_messages(history, sdk_version=SDK_VERSION)
    return RunOut(
        **RunSummary.model_validate(row, from_attributes=True).model_dump(),
        ui_messages=[
            m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in ui_messages
        ],
        messages=run.messages,
    )


@router.delete("/agents/{agent_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(agent_id: int, run_id: int, user: CurrentUser, db: DB) -> None:
    db.delete(get_owned_run(db, user.id, agent_id, run_id))
    db.commit()


def first_prompt(messages: list[ModelMessage]) -> str | None:
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return " ".join(part.content.split())[:120] or None
    return None


def _append_run_messages(run_id: int, new_messages: list[ModelMessage]) -> None:
    """Append one turn's messages to the run's stored history.

    The row lock serializes concurrent turns on the same run (two tabs, two devices), and
    appending to whatever is stored now, rather than writing back the history this request
    loaded, keeps a turn that finished in the meantime from being erased.
    """
    if not new_messages:
        return
    with SessionLocal() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        if run is None:  # deleted mid-stream
            return
        dumped = ModelMessagesTypeAdapter.dump_python(new_messages, mode="json")
        run.messages = [*run.messages, *dumped]
        run.title = run.title or first_prompt(new_messages)
        db.commit()


def reply_messages(messages: list[ModelMessage], turn_id: str) -> list[ModelMessage]:
    """What the model produced this turn: its responses and any tool results.

    The run stamps the user's request and its replies with the turn's run id; the request is
    left out because it was saved before the model was called (see `turn_request`).
    """
    for index, message in enumerate(messages):
        if message.run_id == turn_id:
            turn = messages[index:]
            break
    else:
        return []
    while turn and isinstance(turn[0], ModelRequest):
        turn = turn[1:]
    return turn


def turn_request(
    adapter: VercelAIAdapter, history: list[ModelMessage], turn_id: str, instructions: str
) -> list[ModelMessage]:
    """The client's new message, as the run will see it, to save before the model is called.

    Sanitized like the adapter does (and with client compaction boundaries dropped once there is
    server-side history), and stamped the way the run stamps the request it sends, so the stored
    copy matches what a completed turn would have recorded.
    """
    messages = adapter.sanitize_messages(
        adapter.messages, deferred_tool_results=adapter.deferred_tool_results
    )
    if history:
        messages = [
            replace(m, parts=[p for p in m.parts if not isinstance(p, CompactionPart)])
            if isinstance(m, ModelResponse)
            else m
            for m in messages
        ]
        messages = [m for m in messages if m.parts]
    return [
        replace(m, run_id=turn_id, instructions=instructions)
        if isinstance(m, ModelRequest)
        else replace(m, run_id=turn_id)
        for m in messages
    ]


def trace_metadata(run: AgentRun) -> dict[str, Any]:
    """Attached to the run's OpenTelemetry spans (e.g. for Langfuse filtering and experiments)."""
    version = run.agent_version
    return {
        "agent_id": run.agent_id,
        "agent_name": run.agent.name,
        "agent_version": version.version,
        "agent_version_id": version.id,
        "agent_run_id": run.id,
        "skill_versions": [
            {"skill_id": sv.skill_id, "name": sv.name, "version": sv.version, "id": sv.id}
            for sv in version.skills
        ],
    }


@dataclass(frozen=True)
class ChatContext:
    """Everything a chat turn needs from the database, as plain values."""

    model: str | None
    instructions: str
    history: list[ModelMessage]
    metadata: dict[str, Any]


def load_chat_context(db: Session, user_id: int, agent_id: int, run_id: int) -> ChatContext:
    run = get_owned_run(db, user_id, agent_id, run_id)
    version = run.agent_version
    return ChatContext(
        model=version.model,
        instructions=version.instructions,
        history=ModelMessagesTypeAdapter.validate_python(run.messages),
        metadata=trace_metadata(run),
    )


@router.post("/agents/{agent_id}/runs/{run_id}/chat")
async def run_chat(agent_id: int, run_id: int, request: Request, user: CurrentUser, db: DB):
    """Continue a run. The client sends only its new message; history comes from the database.

    The user's message is saved before the model is called, and the reply when the turn ends,
    whether it completes, fails, or is stopped (the client disconnecting), so the stored history,
    which is all the next turn sees, always matches what the user saw.
    """
    # Sync SQLAlchemy calls go to a worker thread so they don't block the event loop. The `db`
    # session is function-scoped (see `aios.db.DB`), so it's closed and its connection returned
    # to the pool before the response streams; the saves use sessions of their own.
    ctx = await run_in_threadpool(load_chat_context, db, user.id, agent_id, run_id)

    try:
        adapter = await VercelAIAdapter.from_request(
            request, agent=runner, sdk_version=SDK_VERSION, allowed_file_url_schemes=frozenset()
        )
    except ValidationError as e:
        return Response(
            e.json(include_input=False),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            media_type="application/json",
        )

    # Our own run id, so the turn's messages can be picked out of a run that didn't finish.
    turn_id = str(uuid4())
    request_messages = turn_request(adapter, ctx.history, turn_id, ctx.instructions)
    await run_in_threadpool(_append_run_messages, run_id, request_messages)

    saved = False

    async def on_complete(result: AgentRunResult) -> None:
        nonlocal saved
        saved = True
        await run_in_threadpool(
            _append_run_messages, run_id, reply_messages(result.all_messages(), turn_id)
        )

    async def stream() -> AsyncIterator[BaseChunk]:
        # `on_complete` only fires on success: a model error is turned into an error chunk, and
        # the client stopping cancels this task. The captured history includes the partial reply
        # (marked `state='interrupted'`) in both cases.
        with capture_run_messages() as captured:
            try:
                async for chunk in adapter.run_stream(
                    message_history=ctx.history,
                    conversation_id=f"agent-run-{run_id}",
                    run_id=turn_id,
                    model=ctx.model,
                    instructions=ctx.instructions,
                    metadata=ctx.metadata,
                    on_complete=on_complete,
                ):
                    yield chunk
            finally:
                if not saved:
                    with CancelScope(shield=True):
                        await run_in_threadpool(
                            _append_run_messages, run_id, reply_messages(captured, turn_id)
                        )

    return adapter.streaming_response(stream())
