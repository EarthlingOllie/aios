from datetime import datetime
from typing import Annotated, Any

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, UserPromptPart
from pydantic_ai.ui.vercel_ai import VercelAIAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.api.skills import SkillSummary, commit_unique
from aios.auth import CurrentUser
from aios.db import SessionLocal, get_db
from aios.models import Agent, AgentRun, Skill, agent_skills
from aios.runner import build_instructions, runner

router = APIRouter(prefix="/api")
DB = Annotated[Session, Depends(get_db)]

SDK_VERSION = 7


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    base_prompt: str = ""
    skill_ids: list[int] = []


class AgentSummary(BaseModel):
    id: int
    name: str
    description: str
    skill_count: int
    run_count: int
    updated_at: datetime


class AgentOut(BaseModel):
    id: int
    name: str
    description: str
    base_prompt: str
    skills: list[SkillSummary]
    instructions: str
    created_at: datetime
    updated_at: datetime


class RunSummary(BaseModel):
    id: int
    agent_id: int
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


def get_owned_run(db: Session, user_id: int, agent_id: int, run_id: int) -> AgentRun:
    agent = get_owned_agent(db, user_id, agent_id)
    run = db.get(AgentRun, run_id)
    if run is None or run.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return run


def resolve_skills(db: Session, user_id: int, skill_ids: list[int]) -> list[Skill]:
    ids = set(skill_ids)
    skills = db.scalars(select(Skill).where(Skill.owner_id == user_id, Skill.id.in_(ids))).all()
    if len(skills) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown skill id")
    return list(skills)


def agent_out(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        base_prompt=agent.base_prompt,
        skills=[SkillSummary.model_validate(s, from_attributes=True) for s in agent.skills],
        instructions=build_instructions(agent),
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def run_summary(run: AgentRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        agent_id=run.agent_id,
        title=run.title,
        message_count=len(run.messages),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


# --- Agents -------------------------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(user: CurrentUser, db: DB):
    skill_count = (
        select(func.count())
        .select_from(agent_skills)
        .where(agent_skills.c.agent_id == Agent.id)
        .scalar_subquery()
    )
    run_count = (
        select(func.count(AgentRun.id)).where(AgentRun.agent_id == Agent.id).scalar_subquery()
    )
    rows = db.execute(
        select(Agent, skill_count, run_count).where(Agent.owner_id == user.id).order_by(Agent.name)
    ).all()
    return [
        AgentSummary(
            id=a.id,
            name=a.name,
            description=a.description,
            skill_count=skills,
            run_count=runs,
            updated_at=a.updated_at,
        )
        for a, skills, runs in rows
    ]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(body: AgentIn, user: CurrentUser, db: DB):
    agent = Agent(
        owner_id=user.id,
        name=body.name,
        description=body.description,
        base_prompt=body.base_prompt,
        skills=resolve_skills(db, user.id, body.skill_ids),
    )
    db.add(agent)
    commit_unique(db, "an agent")
    return agent_out(agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: int, user: CurrentUser, db: DB):
    return agent_out(get_owned_agent(db, user.id, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: int, body: AgentIn, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    agent.name = body.name
    agent.description = body.description
    agent.base_prompt = body.base_prompt
    agent.skills = resolve_skills(db, user.id, body.skill_ids)
    commit_unique(db, "an agent")
    return agent_out(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: int, user: CurrentUser, db: DB) -> None:
    db.delete(get_owned_agent(db, user.id, agent_id))
    db.commit()


# --- Runs ---------------------------------------------------------------------------------------


@router.get("/agents/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: int, user: CurrentUser, db: DB):
    agent = get_owned_agent(db, user.id, agent_id)
    rows = db.execute(
        select(
            AgentRun.id,
            AgentRun.agent_id,
            AgentRun.title,
            func.jsonb_array_length(AgentRun.messages).label("message_count"),
            AgentRun.created_at,
            AgentRun.updated_at,
        )
        .where(AgentRun.agent_id == agent.id)
        .order_by(AgentRun.updated_at.desc())
    ).all()
    return [RunSummary.model_validate(row, from_attributes=True) for row in rows]


@router.post(
    "/agents/{agent_id}/runs", response_model=RunSummary, status_code=status.HTTP_201_CREATED
)
def create_run(agent_id: int, user: CurrentUser, db: DB):
    run = AgentRun(agent=get_owned_agent(db, user.id, agent_id), messages=[])
    db.add(run)
    db.commit()
    return run_summary(run)


@router.get("/agents/{agent_id}/runs/{run_id}", response_model=RunOut)
def get_run(agent_id: int, run_id: int, user: CurrentUser, db: DB):
    run = get_owned_run(db, user.id, agent_id, run_id)
    history = ModelMessagesTypeAdapter.validate_python(run.messages)
    ui_messages = VercelAIAdapter.dump_messages(history, sdk_version=SDK_VERSION)
    return RunOut(
        **run_summary(run).model_dump(),
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


def save_history(run_id: int, messages: list[ModelMessage]) -> None:
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        if run is None:  # deleted mid-stream
            return
        run.messages = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
        run.title = run.title or first_prompt(messages)
        db.commit()


@router.post("/agents/{agent_id}/runs/{run_id}/chat")
async def run_chat(agent_id: int, run_id: int, request: Request, user: CurrentUser, db: DB):
    """Continue a run. The client sends only its new message; history comes from the database."""
    run = get_owned_run(db, user.id, agent_id, run_id)
    history = ModelMessagesTypeAdapter.validate_python(run.messages)
    instructions = build_instructions(run.agent)

    async def on_complete(result: AgentRunResult) -> None:
        await to_thread.run_sync(save_history, run_id, result.all_messages())

    return await VercelAIAdapter.dispatch_request(
        request,
        agent=runner,
        sdk_version=SDK_VERSION,
        message_history=history,
        instructions=instructions,
        on_complete=on_complete,
        allowed_file_url_schemes=frozenset(),
    )
