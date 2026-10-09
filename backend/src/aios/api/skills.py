from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios.auth import CurrentUser
from aios.db import get_db
from aios.models import Agent, Skill, agent_skills

router = APIRouter(prefix="/api")
DB = Annotated[Session, Depends(get_db)]


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    content: str = ""


class SkillSummary(BaseModel):
    id: int
    name: str
    description: str
    updated_at: datetime


class AgentRef(BaseModel):
    id: int
    name: str


class SkillOut(SkillSummary):
    content: str
    created_at: datetime
    agents: list[AgentRef]


def get_owned_skill(db: Session, user_id: int, skill_id: int) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None or skill.owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return skill


def commit_unique(db: Session, what: str) -> None:
    """Commit, turning a (owner_id, name) unique violation into a 409."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"You already have {what} with that name."
        ) from exc


def skill_out(db: Session, skill: Skill) -> SkillOut:
    agents = db.execute(
        select(Agent.id, Agent.name)
        .join(agent_skills, agent_skills.c.agent_id == Agent.id)
        .where(agent_skills.c.skill_id == skill.id)
        .order_by(Agent.name)
    ).all()
    return SkillOut(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        agents=[AgentRef(id=a.id, name=a.name) for a in agents],
    )


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(user: CurrentUser, db: DB):
    return db.scalars(select(Skill).where(Skill.owner_id == user.id).order_by(Skill.name)).all()


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillIn, user: CurrentUser, db: DB):
    skill = Skill(owner_id=user.id, **body.model_dump())
    db.add(skill)
    commit_unique(db, "a skill")
    return skill_out(db, skill)


@router.get("/skills/{skill_id}", response_model=SkillOut)
def get_skill(skill_id: int, user: CurrentUser, db: DB):
    return skill_out(db, get_owned_skill(db, user.id, skill_id))


@router.put("/skills/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: int, body: SkillIn, user: CurrentUser, db: DB):
    skill = get_owned_skill(db, user.id, skill_id)
    for key, value in body.model_dump().items():
        setattr(skill, key, value)
    commit_unique(db, "a skill")
    return skill_out(db, skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: int, user: CurrentUser, db: DB) -> None:
    # Detaches the skill from any agents using it (agent_skills cascades).
    db.delete(get_owned_skill(db, user.id, skill_id))
    db.commit()
