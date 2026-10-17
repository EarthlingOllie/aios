from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Agent,
    AgentVersion,
    Skill,
    SkillVersion,
    Workflow,
    agent_version_skills,
)
from aios.versioning import save_skill_version

router = APIRouter(prefix="/api")


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    content: str = ""
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class SkillSummary(BaseModel):
    id: int
    name: str
    description: str
    version: int
    version_id: int
    updated_at: datetime


class SkillVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime


class SkillVersionOut(SkillVersionSummary):
    name: str
    description: str
    content: str


class SkillUsage(BaseModel):
    """A live agent whose current version uses some version of this skill."""

    agent_id: int
    agent_name: str
    agent_version: int
    skill_version: int


class SkillOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    current_version: SkillVersionOut
    agents: list[SkillUsage]


def get_owned_skill(db: Session, user_id: int, skill_id: int) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None or skill.owner_id != user_id or skill.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return skill


def lock_live(
    db: Session,
    model: type[Skill] | type[Agent] | type[Workflow],
    ids: Iterable[int],
    *,
    read: bool = False,
) -> set[int]:
    """Row-lock these skills, agents or workflows (in id order) and return the ids still live.

    Saving something that depends on a row (an agent attaching a skill, a workflow running an
    agent or another workflow) and deleting that row both take this lock, so a delete can't slip
    in between the save's liveness check and its commit: it either waits for the save (and then
    sees the usage) or wins, and the save sees the row is gone. A row deleted while we waited for
    its lock isn't returned: the lock's re-check reads the committed `deleted_at`.

    `read` takes a share lock instead, for something that only needs the row to stay live (e.g.
    starting a run). Locks are FOR UPDATE otherwise, so a delete and a save taking the same row
    wait for each other.

    Lock order, to avoid deadlocks: skills, then agents, then workflows, each in ascending id.
    An agent save locks its skills, then the agent row (here, then again in save_agent_version);
    a workflow save locks the agents it runs, then the workflows it runs together with itself
    (`aios.api.workflows.lock_graph`), then its own row again in save_workflow_version. A delete
    locks only the row it deletes. Nothing locks in the other order.
    """
    ids = sorted(set(ids))
    if not ids:
        return set()
    return set(
        db.scalars(
            select(model.id)
            .where(model.id.in_(ids), model.deleted_at.is_(None))
            .order_by(model.id)
            .with_for_update(read=read)
        )
    )


def lock_live_skills(db: Session, skill_ids: Iterable[int]) -> set[int]:
    """Row-lock these skills and return the ids still live (see `lock_live`)."""
    return lock_live(db, Skill, skill_ids)


def get_skill_version(db: Session, skill: Skill, number: int) -> SkillVersion:
    version = db.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id, SkillVersion.version == number
        )
    )
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return version


@contextmanager
def unique_name(db: Session, what: str, index: str, detail: str | None = None) -> Iterator[None]:
    """Turn a violation of the live-name unique index `index` inside the block into a 409.

    Any other integrity error is a bug, not a name clash, and propagates. The 409 says the user
    already has `what` with that name, unless `detail` says otherwise (names unique among
    everyone's, say).
    """
    try:
        yield
    except IntegrityError as exc:
        diag = getattr(exc.orig, "diag", None)
        if getattr(diag, "constraint_name", None) != index:
            raise
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail or f"You already have {what} with that name."
        ) from exc


def skill_usage(db: Session, skill_id: int) -> list[SkillUsage]:
    rows = db.execute(
        select(Agent.id, Agent.name, AgentVersion.version, SkillVersion.version)
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .join(agent_version_skills, agent_version_skills.c.agent_version_id == AgentVersion.id)
        .join(SkillVersion, SkillVersion.id == agent_version_skills.c.skill_version_id)
        .where(SkillVersion.skill_id == skill_id, Agent.deleted_at.is_(None))
        .order_by(Agent.name)
    ).all()
    return [
        SkillUsage(agent_id=a, agent_name=n, agent_version=av, skill_version=sv)
        for a, n, av, sv in rows
    ]


def skill_out(db: Session, skill: Skill) -> SkillOut:
    return SkillOut(
        id=skill.id,
        name=skill.name,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        current_version=SkillVersionOut.model_validate(skill.current_version, from_attributes=True),
        agents=skill_usage(db, skill.id),
    )


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(user: CurrentUser, db: DB):
    rows = db.execute(
        select(Skill, SkillVersion)
        .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
        .where(Skill.owner_id == user.id, Skill.deleted_at.is_(None))
        .order_by(Skill.name)
    ).all()
    return [
        SkillSummary(
            id=skill.id,
            name=skill.name,
            description=v.description,
            version=v.version,
            version_id=v.id,
            updated_at=skill.updated_at,
        )
        for skill, v in rows
    ]


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillIn, user: CurrentUser, db: DB):
    skill = Skill(owner_id=user.id, name=body.name)
    db.add(skill)
    with unique_name(db, "a skill", "uq_skills_owner_id_name_live"):
        save_skill_version(
            db,
            skill,
            description=body.description,
            content=body.content,
            note=body.note,
            user_id=user.id,
        )
        db.commit()
    return skill_out(db, skill)


@router.get("/skills/{skill_id}", response_model=SkillOut)
def get_skill(skill_id: DbId, user: CurrentUser, db: DB):
    return skill_out(db, get_owned_skill(db, user.id, skill_id))


@router.put("/skills/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: DbId, body: SkillIn, user: CurrentUser, db: DB):
    skill = get_owned_skill(db, user.id, skill_id)
    skill.name = body.name
    with unique_name(db, "a skill", "uq_skills_owner_id_name_live"):
        save_skill_version(
            db,
            skill,
            description=body.description,
            content=body.content,
            note=body.note,
            user_id=user.id,
        )
        db.commit()
    return skill_out(db, skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: DbId, user: CurrentUser, db: DB) -> None:
    skill = get_owned_skill(db, user.id, skill_id)
    # Hold the row lock across the usage check and the delete; agent saves take it too.
    if not lock_live_skills(db, [skill.id]):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    usage = skill_usage(db, skill.id)
    if usage:
        names = ", ".join(u.agent_name for u in usage)
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Remove this skill from these agents first: {names}."
        )
    # Soft delete: older agent versions may still pin this skill's versions.
    skill.deleted_at = func.now()
    db.commit()


@router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionSummary])
def list_skill_versions(skill_id: DbId, user: CurrentUser, db: DB):
    skill = get_owned_skill(db, user.id, skill_id)
    return db.scalars(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
    ).all()


@router.get("/skills/{skill_id}/versions/{number}", response_model=SkillVersionOut)
def get_skill_version_detail(skill_id: DbId, number: DbId, user: CurrentUser, db: DB):
    return get_skill_version(db, get_owned_skill(db, user.id, skill_id), number)


@router.post("/skills/{skill_id}/versions/{number}/restore", response_model=SkillOut)
def restore_skill_version(skill_id: DbId, number: DbId, user: CurrentUser, db: DB):
    """Make an old version's content current again, as a new version."""
    skill = get_owned_skill(db, user.id, skill_id)
    old = get_skill_version(db, skill, number)
    save_skill_version(
        db,
        skill,
        description=old.description,
        content=old.content,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    db.commit()
    return skill_out(db, skill)
