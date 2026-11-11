from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aios.api.provenance import (
    Archived,
    Provenance,
    UserOut,
    archived,
    built_by,
    people,
    provenance,
)
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores.models import (
    Agent,
    AgentVersion,
    Skill,
    SkillVersion,
    Workflow,
    agent_version_skills,
)
from aios.db import DB, DbId
from aios.permissions import Action, allow
from aios.versioning import save_skill_version

router = APIRouter(prefix="/api")

SKILL_NAME_INDEX = "uq_skills_name"


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"\S")
    description: str = ""
    content: str = ""
    # Optional "what changed", stored on the new version if one is created.
    note: str | None = None


class SkillSummary(Provenance, Archived):
    id: int
    name: str
    description: str
    version: int
    version_id: int
    updated_at: datetime
    # How many agents that aren't archived use some version of it (see `skill_uses`).
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
    """An agent (not archived) whose current version uses some version of this skill."""

    agent_id: int
    agent_name: str
    agent_version: int
    skill_version: int


class SkillOut(Provenance, Archived):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    current_version: SkillVersionOut
    # What uses it (as `GET .../used-by` lists it).
    agents: list[SkillUsage]


class SkillUsedBy(BaseModel):
    """What uses a skill, as its archive confirmation shows it: the agents whose current version
    uses some version of it, which keep running on it once it's archived."""

    agents: list[SkillUsage]


def get_org_skill(store: Session, skill_id: int) -> Skill:
    """The skill in the organisation's store (`store`), archived or not; a 404 otherwise. Ids are
    the store's own, so another organisation's skill is never reached, whatever its id."""
    skill = store.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return skill


def lock_live(
    db: Session,
    model: type[Skill] | type[Agent] | type[Workflow],
    ids: Iterable[int],
    *,
    read: bool = False,
) -> set[int]:
    """Row-lock these skills, agents or workflows (in id order) and return the ids of those
    that aren't archived ("live").

    Saving something that depends on a row (an agent attaching a skill, a workflow running an
    agent or another workflow) and archiving that row both take this lock, so an archive can't
    slip in between the save's check and its commit: it either waits for the save or wins, and
    the save sees the row is archived. A row archived while we waited for its lock isn't
    returned: the lock's re-check reads the committed `archived_at`.

    `read` takes a share lock instead, for something that only needs the row to stay live (e.g.
    starting a run). Locks are FOR UPDATE otherwise, so an archive and a save taking the same
    row wait for each other.

    Lock order, to avoid deadlocks: skills, then agents, then workflows, each in ascending id.
    An agent save locks its skills, then the agent row (here, then again in save_agent_version);
    a workflow save locks the agents it runs, then the workflows it runs together with itself
    (`aios.api.workflows.lock_graph`), then its own row again in save_workflow_version. An
    archive locks only the row it archives. Nothing locks in the other order.
    """
    ids = sorted(set(ids))
    if not ids:
        return set()
    return set(
        db.scalars(
            select(model.id)
            .where(model.id.in_(ids), model.archived_at.is_(None))
            .order_by(model.id)
            .with_for_update(read=read)
        )
    )


def lock_skills(db: Session, skill_ids: Iterable[int]) -> list[Skill]:
    """Row-lock these skills, archived or not, in id order (see `lock_live`), refreshing them,
    so whether each is archived is as committed."""
    ids = sorted(set(skill_ids))
    if not ids:
        return []
    return list(
        db.scalars(
            select(Skill)
            .where(Skill.id.in_(ids))
            .order_by(Skill.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


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
    """Turn a violation of the name unique index `index` inside the block into a 409.

    The 409 says the organisation already has `what` with that name, unless `detail` says
    otherwise (names unique among everyone's, say).
    """
    with on_conflict(db, index, detail or f"The organisation already has {what} with that name."):
        yield


def skill_uses(*columns) -> Select:
    """`columns` over each use of a skill by the current version of an agent that isn't
    archived: one row per agent and skill version it pins, joining `Agent`, its current
    `AgentVersion` and that `SkillVersion`: what uses the skill."""
    return (
        select(*columns)
        .select_from(Agent)
        .join(AgentVersion, AgentVersion.id == Agent.current_version_id)
        .join(agent_version_skills, agent_version_skills.c.agent_version_id == AgentVersion.id)
        .join(SkillVersion, SkillVersion.id == agent_version_skills.c.skill_version_id)
        .where(Agent.archived_at.is_(None))
    )


def skill_usage(store: Session, skill_id: int) -> list[SkillUsage]:
    rows = store.execute(
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


def skill_out(db: Session, org_id: int, store: Session, skill: Skill) -> SkillOut:
    """The skill (in the organisation `org_id`'s store, `store`), with who made, changed and
    archived it read through the platform session `db`."""
    version = skill.current_version
    known = people(
        db,
        org_id,
        [skill.created_by, skill.updated_by, skill.archived_by, version.created_by],
    )
    return SkillOut(
        id=skill.id,
        name=skill.name,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        current_version=version_summary(version, known, SkillVersionOut),
        agents=skill_usage(store, skill.id),
        **provenance(db, skill, known),
        **archived(skill, known),
    )


@router.get("/skills", response_model=list[SkillSummary], dependencies=[allow(Action.read)])
def list_skills(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    archived_only: Annotated[bool, Query(alias="archived")] = False,
):
    """The organisation's skills that aren't archived, by name; with `archived`, the archived
    ones instead."""
    uses = (
        skill_uses(SkillVersion.skill_id, func.count(Agent.id.distinct()).label("agents"))
        .group_by(SkillVersion.skill_id)
        .subquery()
    )
    rows = store.execute(
        select(Skill, SkillVersion, func.coalesce(uses.c.agents, 0))
        .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
        .outerjoin(uses, uses.c.skill_id == Skill.id)
        .where(Skill.archived_at.is_not(None) if archived_only else Skill.archived_at.is_(None))
        .order_by(Skill.name)
    ).all()
    known = people(
        db, org.id, [i for s, _, _ in rows for i in (s.created_by, s.updated_by, s.archived_by)]
    )
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
            **archived(skill, known),
        )
        for skill, v, agents in rows
    ]


@router.post(
    "/skills",
    response_model=SkillOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.build)],
)
def create_skill(
    body: SkillIn, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    skill = Skill(**built_by(user.id), name=body.name)
    store.add(skill)
    with unique_name(store, "a skill", SKILL_NAME_INDEX):
        save_skill_version(
            store,
            skill,
            description=body.description,
            content=body.content,
            note=body.note,
            user_id=user.id,
        )
        store.commit()
    return skill_out(db, org.id, store, skill)


@router.get("/skills/{skill_id}", response_model=SkillOut, dependencies=[allow(Action.read)])
def get_skill(skill_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    return skill_out(db, org.id, store, get_org_skill(store, skill_id))


@router.put("/skills/{skill_id}", response_model=SkillOut, dependencies=[allow(Action.build)])
def update_skill(
    skill_id: DbId,
    body: SkillIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    skill = get_org_skill(store, skill_id)
    skill.name = body.name
    skill.updated_by = user.id
    with unique_name(store, "a skill", SKILL_NAME_INDEX):
        save_skill_version(
            store,
            skill,
            description=body.description,
            content=body.content,
            note=body.note,
            user_id=user.id,
        )
        store.commit()
    return skill_out(db, org.id, store, skill)


@router.post(
    "/skills/{skill_id}/archive", response_model=SkillOut, dependencies=[allow(Action.build)]
)
def archive_skill(
    skill_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Archive the skill: it can't be added to a new agent version. Never refused, even while
    agents use it (`GET .../used-by`): the versions that pin it keep running on it. Archiving
    an archived skill changes nothing."""
    skill = get_org_skill(store, skill_id)
    # Agent saves lock the skills they attach (`resolve_skill_versions`), so one in flight is
    # waited for, or sees this archive.
    lock_skills(store, [skill.id])
    if skill.archived_at is None:
        skill.archived_at = func.now()
        skill.archived_by = user.id
        store.commit()
    return skill_out(db, org.id, store, skill)


@router.post(
    "/skills/{skill_id}/unarchive", response_model=SkillOut, dependencies=[allow(Action.build)]
)
def unarchive_skill(skill_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    """Bring the skill back: agent versions can include it again."""
    skill = get_org_skill(store, skill_id)
    lock_skills(store, [skill.id])
    skill.archived_at = None
    skill.archived_by = None
    store.commit()
    return skill_out(db, org.id, store, skill)


@router.get(
    "/skills/{skill_id}/used-by", response_model=SkillUsedBy, dependencies=[allow(Action.read)]
)
def get_skill_used_by(skill_id: DbId, store: CurrentStore):
    """What uses the skill, for the archive confirmation to show: read-only, as archiving is
    never refused."""
    return SkillUsedBy(agents=skill_usage(store, get_org_skill(store, skill_id).id))


@router.get(
    "/skills/{skill_id}/versions",
    response_model=list[SkillVersionSummary],
    dependencies=[allow(Action.read)],
)
def list_skill_versions(skill_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    skill = get_org_skill(store, skill_id)
    versions = store.scalars(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
    ).all()
    known = people(db, org.id, [v.created_by for v in versions])
    return [version_summary(v, known, SkillVersionSummary) for v in versions]


@router.get(
    "/skills/{skill_id}/versions/{number}",
    response_model=SkillVersionOut,
    dependencies=[allow(Action.read)],
)
def get_skill_version_detail(
    skill_id: DbId, number: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    version = get_skill_version(store, get_org_skill(store, skill_id), number)
    known = people(db, org.id, [version.created_by])
    return version_summary(version, known, SkillVersionOut)


@router.post(
    "/skills/{skill_id}/versions/{number}/restore",
    response_model=SkillOut,
    dependencies=[allow(Action.build)],
)
def restore_skill_version(
    skill_id: DbId,
    number: DbId,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Make an old version's content current again, as a new version."""
    skill = get_org_skill(store, skill_id)
    old = get_skill_version(store, skill, number)
    skill.updated_by = user.id
    save_skill_version(
        store,
        skill,
        description=old.description,
        content=old.content,
        note=f"Restored from v{old.version}",
        user_id=user.id,
    )
    store.commit()
    return skill_out(db, org.id, store, skill)
