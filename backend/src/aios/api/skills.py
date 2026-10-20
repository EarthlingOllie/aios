from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios.api.provenance import Provenance, UserOut, made_by, people, provenance
from aios.auth import CurrentOrganisation, CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Agent,
    AgentVersion,
    Organisation,
    Skill,
    SkillVersion,
    Workflow,
    agent_version_skills,
)
from aios.versioning import save_skill_version

router = APIRouter(prefix="/api")

SKILL_NAME_INDEX = "uq_skills_organisation_id_name_live"


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    content: str = ""
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class SkillSummary(Provenance):
    id: int
    name: str
    description: str
    version: int
    version_id: int
    updated_at: datetime
    # How many live agents use some version of it (see `skill_uses`).
    agent_count: int


class SkillVersionSummary(BaseModel):
    id: int
    version: int
    note: str | None
    created_at: datetime
    # The member who saved it.
    created_by: UserOut | None


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


class SkillOut(Provenance):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    current_version: SkillVersionOut
    agents: list[SkillUsage]


def get_org_skill(db: Session, org: Organisation, skill_id: int) -> Skill:
    """The organisation's live skill; a 404 otherwise, as if it didn't exist."""
    skill = db.get(Skill, skill_id)
    if skill is None or skill.organisation_id != org.id or skill.deleted_at is not None:
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
def on_conflict(db: Session, constraint: str, detail: str) -> Iterator[None]:
    """Turn a violation of the unique constraint or index `constraint` inside the block into a
    409 saying `detail`. Any other integrity error is a bug, not a clash, and propagates."""
    try:
        yield
    except IntegrityError as exc:
        diag = getattr(exc.orig, "diag", None)
        if getattr(diag, "constraint_name", None) != constraint:
            raise
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc


@contextmanager
def unique_name(db: Session, what: str, index: str, detail: str | None = None) -> Iterator[None]:
    """Turn a violation of the live-name unique index `index` inside the block into a 409.

    The 409 says the organisation already has `what` with that name, unless `detail` says
    otherwise (names unique among everyone's, say).
    """
    with on_conflict(db, index, detail or f"The organisation already has {what} with that name."):
        yield


def skill_uses(*columns) -> Select:
    """`columns` over each use of a skill by a live agent's current version: one row per agent
    and skill version it pins, joining `Agent`, its current `AgentVersion` and that
    `SkillVersion`. A skill with such a use is in use, and can't be deleted."""
    return (
        select(*columns)
        .select_from(Agent)
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .join(agent_version_skills, agent_version_skills.c.agent_version_id == AgentVersion.id)
        .join(SkillVersion, SkillVersion.id == agent_version_skills.c.skill_version_id)
        .where(Agent.deleted_at.is_(None))
    )


def skill_usage(db: Session, skill_id: int) -> list[SkillUsage]:
    rows = db.execute(
        skill_uses(Agent.id, Agent.name, AgentVersion.version, SkillVersion.version)
        .where(SkillVersion.skill_id == skill_id)
        .order_by(Agent.name)
    ).all()
    return [
        SkillUsage(agent_id=a, agent_name=n, agent_version=av, skill_version=sv)
        for a, n, av, sv in rows
    ]


def version_summary(
    version: SkillVersion, known: dict[int, UserOut], model: type[SkillVersionSummary]
) -> SkillVersionSummary:
    """The version as `model`, with who saved it from `known` (see `provenance.people`)."""
    fields = {name: getattr(version, name) for name in model.model_fields if name != "created_by"}
    return model(**fields, created_by=known.get(version.created_by))


def skill_out(db: Session, skill: Skill) -> SkillOut:
    version = skill.current_version
    known = people(db, [skill.created_by, skill.updated_by, version.created_by])
    return SkillOut(
        id=skill.id,
        name=skill.name,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        current_version=version_summary(version, known, SkillVersionOut),
        agents=skill_usage(db, skill.id),
        **provenance(db, skill, known),
    )


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(org: CurrentOrganisation, db: DB):
    uses = (
        skill_uses(SkillVersion.skill_id, func.count(Agent.id.distinct()).label("agents"))
        # Agents pin only their organisation's skills, so this keeps the count to its rows.
        .where(Agent.organisation_id == org.id)
        .group_by(SkillVersion.skill_id)
        .subquery()
    )
    rows = db.execute(
        select(Skill, SkillVersion, func.coalesce(uses.c.agents, 0))
        .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
        .outerjoin(uses, uses.c.skill_id == Skill.id)
        .where(Skill.organisation_id == org.id, Skill.deleted_at.is_(None))
        .order_by(Skill.name)
    ).all()
    known = people(db, [i for skill, _, _ in rows for i in (skill.created_by, skill.updated_by)])
    return [
        SkillSummary(
            id=skill.id,
            name=skill.name,
            description=v.description,
            version=v.version,
            version_id=v.id,
            updated_at=skill.updated_at,
            agent_count=agents,
            **provenance(db, skill, known),
        )
        for skill, v, agents in rows
    ]


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
def create_skill(body: SkillIn, user: CurrentUser, org: CurrentOrganisation, db: DB):
    skill = Skill(**made_by(org.id, user.id), name=body.name)
    db.add(skill)
    with unique_name(db, "a skill", SKILL_NAME_INDEX):
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
def get_skill(skill_id: DbId, org: CurrentOrganisation, db: DB):
    return skill_out(db, get_org_skill(db, org, skill_id))


@router.put("/skills/{skill_id}", response_model=SkillOut)
def update_skill(
    skill_id: DbId, body: SkillIn, user: CurrentUser, org: CurrentOrganisation, db: DB
):
    skill = get_org_skill(db, org, skill_id)
    skill.name = body.name
    skill.updated_by = user.id
    with unique_name(db, "a skill", SKILL_NAME_INDEX):
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
def delete_skill(skill_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB) -> None:
    skill = get_org_skill(db, org, skill_id)
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
    skill.deleted_by = user.id
    db.commit()


@router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionSummary])
def list_skill_versions(skill_id: DbId, org: CurrentOrganisation, db: DB):
    skill = get_org_skill(db, org, skill_id)
    versions = db.scalars(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
    ).all()
    known = people(db, [v.created_by for v in versions])
    return [version_summary(v, known, SkillVersionSummary) for v in versions]


@router.get("/skills/{skill_id}/versions/{number}", response_model=SkillVersionOut)
def get_skill_version_detail(skill_id: DbId, number: DbId, org: CurrentOrganisation, db: DB):
    version = get_skill_version(db, get_org_skill(db, org, skill_id), number)
    return version_summary(version, people(db, [version.created_by]), SkillVersionOut)


@router.post("/skills/{skill_id}/versions/{number}/restore", response_model=SkillOut)
def restore_skill_version(
    skill_id: DbId, number: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB
):
    """Make an old version's content current again, as a new version."""
    skill = get_org_skill(db, org, skill_id)
    old = get_skill_version(db, skill, number)
    skill.updated_by = user.id
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
