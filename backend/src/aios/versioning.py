"""Creates the immutable versions behind agents and skills.

Saving only creates a version when the behaviour-affecting config changed (compared by hash), so
renames and no-op saves don't bump the version. The parent row is locked while the next number
is allocated, so concurrent saves can't both claim it. Callers commit.
"""

import hashlib
import json
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from aios.models import Agent, AgentVersion, Skill, SkillVersion, agent_version_skills

DEFAULT_BASE_PROMPT = "You are a helpful assistant."


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def render_instructions(base_prompt: str, skills: list[SkillVersion]) -> str:
    sections = [base_prompt.strip() or DEFAULT_BASE_PROMPT]
    if skills:
        sections.append(
            "You have the following skills. When a request matches a skill's description, "
            "follow that skill's instructions."
        )
        for skill in skills:
            body = "\n\n".join(s for s in (skill.description.strip(), skill.content.strip()) if s)
            sections.append(f'<skill name="{skill.skill.name}">\n{body}\n</skill>')
    return "\n\n".join(sections)


def _lock_current_version_id(
    db: Session, model: type[Agent] | type[Skill], row_id: int
) -> int | None:
    """Locks the parent row and returns its committed current_version_id.

    Read from the locked row rather than the ORM relationship: a concurrent save may have
    committed a new current version while we waited, and the identity map would still show
    the old one.
    """
    return db.scalar(select(model.current_version_id).where(model.id == row_id).with_for_update())


def save_skill_version(
    db: Session,
    skill: Skill,
    *,
    description: str,
    content: str,
    note: str | None,
    user_id: int,
) -> SkillVersion:
    digest = config_hash({"description": description, "content": content})
    db.flush()  # assigns skill.id for a new skill
    # Compare against the current version only once the lock is held, so two identical
    # concurrent saves can't both see a change and create duplicate versions.
    current_id = _lock_current_version_id(db, Skill, skill.id)
    current = db.get(SkillVersion, current_id) if current_id is not None else None
    if current is not None and current.content_hash == digest:
        db.refresh(skill, ["current_version_id", "current_version"])  # drop stale state
        return current
    number = db.scalar(
        select(func.coalesce(func.max(SkillVersion.version), 0)).where(
            SkillVersion.skill_id == skill.id
        )
    )
    version = SkillVersion(
        skill=skill,
        version=number + 1,
        description=description,
        content=content,
        content_hash=digest,
        note=note,
        created_by=user_id,
    )
    db.add(version)
    skill.current_version = version
    skill.updated_at = func.now()
    return version


def save_agent_version(
    db: Session,
    agent: Agent,
    *,
    base_prompt: str,
    skills: list[SkillVersion],
    note: str | None,
    user_id: int,
    model: str | None = None,
) -> AgentVersion:
    digest = config_hash(
        {
            "base_prompt": base_prompt,
            "model": model,
            "skill_version_ids": [s.id for s in skills],
        }
    )
    db.flush()  # assigns agent.id for a new agent
    # Compare against the current version only once the lock is held (see save_skill_version).
    current_id = _lock_current_version_id(db, Agent, agent.id)
    current = db.get(AgentVersion, current_id) if current_id is not None else None
    if current is not None and current.config_hash == digest:
        db.refresh(agent, ["current_version_id", "current_version"])
        return current
    number = db.scalar(
        select(func.coalesce(func.max(AgentVersion.version), 0)).where(
            AgentVersion.agent_id == agent.id
        )
    )
    version = AgentVersion(
        agent=agent,
        version=number + 1,
        base_prompt=base_prompt,
        model=model,
        instructions=render_instructions(base_prompt, skills),
        config_hash=digest,
        note=note,
        created_by=user_id,
    )
    db.add(version)
    db.flush()
    if skills:
        db.execute(
            insert(agent_version_skills),
            [
                {"agent_version_id": version.id, "skill_version_id": s.id, "position": i}
                for i, s in enumerate(skills)
            ],
        )
    db.expire(version, ["skills"])
    agent.current_version = version
    agent.updated_at = func.now()
    return version
