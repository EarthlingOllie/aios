"""Creates the immutable versions behind agents, skills and workflows.

Saving only creates a version when the behaviour-affecting config changed (compared by hash), so
no-op saves and agent renames don't bump the version. A skill's name is rendered into agent
instructions, so it's snapshotted on the skill version and renaming a skill does create one. The
parent row is locked while the next number is allocated, so concurrent saves can't both claim it.
Callers commit.
"""

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from aios.models import (
    Agent,
    AgentVersion,
    AgentVersionTool,
    Skill,
    SkillVersion,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowVersion,
    agent_version_skills,
)

DEFAULT_BASE_PROMPT = "You are a helpful assistant."

# `<skill` / `</skill` tags (any case, any spacing after `<`) inside a skill's text.
_SKILL_TAG = re.compile(r"<(?=\s*/?\s*skill\b)", re.IGNORECASE)


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
            sections.append(render_skill_block(skill.name, skill.description, skill.content))
    return "\n\n".join(sections)


def render_skill_block(name: str, description: str, content: str) -> str:
    """Wraps one skill in a `<skill name="...">` block that its own text can't break out of.

    The name is attribute-escaped. The body only has `<` escaped where it starts a `skill` tag,
    so content can neither close the block early nor open a fake one. Other markup (code,
    HTML, `a < b`) is left as written: escaping every `<` and `&` would make skills harder for
    the model to read, and only the block's own tags matter for where it ends.
    """
    body = "\n\n".join(s for s in (description.strip(), content.strip()) if s)
    body = _SKILL_TAG.sub("&lt;", body)
    return f'<skill name="{html.escape(name, quote=True)}">\n{body}\n</skill>'


def _lock_current_version_id(
    db: Session, model: type[Agent] | type[Skill] | type[Workflow], row_id: int
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
    # The name comes from the skill row (callers set it first) and is snapshotted with the content.
    name = skill.name
    digest = config_hash({"name": name, "description": description, "content": content})
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
        name=name,
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
    tools: Sequence[tuple[str, dict[str, Any]]] = (),
) -> AgentVersion:
    """`tools` are (key, settings) pairs, already validated (`aios.tools.validate_tool`)."""
    config: dict[str, Any] = {
        "base_prompt": base_prompt,
        "model": model,
        "skill_version_ids": [s.id for s in skills],
    }
    # Only hashed when present, so versions saved before tools existed keep their hash and
    # re-saving them unchanged doesn't create a version.
    if tools:
        config["tools"] = [{"key": key, "config": settings} for key, settings in tools]
    digest = config_hash(config)
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
        tools=[
            AgentVersionTool(tool_key=key, config=settings, position=i)
            for i, (key, settings) in enumerate(tools)
        ],
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


@dataclass(frozen=True)
class NodeSpec:
    """A workflow node to save: its key, and the agent or workflow version it pins."""

    key: str
    kind: WorkflowNodeKind
    version_id: int


def save_workflow_version(
    db: Session,
    workflow: Workflow,
    *,
    nodes: Sequence[NodeSpec],
    edges: Sequence[tuple[str, str]],
    note: str | None,
    user_id: int,
) -> WorkflowVersion:
    """`nodes` and `edges` are already validated (`aios.workflows.validate_graph`).

    The hash covers the graph, not the order it was sent in: nodes are sorted by key (unique
    within a graph) and edges by (source, target), so listing them differently is a no-op save.
    The stored `position`s keep the order of the version that was created; a pure reorder
    creates no version, so it isn't stored either.
    """
    digest = config_hash(
        {
            "nodes": sorted(
                ({"key": n.key, "kind": n.kind, "version_id": n.version_id} for n in nodes),
                key=lambda n: n["key"],
            ),
            "edges": sorted([source, target] for source, target in edges),
        }
    )
    db.flush()  # assigns workflow.id for a new workflow
    # Compare against the current version only once the lock is held (see save_skill_version).
    current_id = _lock_current_version_id(db, Workflow, workflow.id)
    current = db.get(WorkflowVersion, current_id) if current_id is not None else None
    if current is not None and current.config_hash == digest:
        db.refresh(workflow, ["current_version_id", "current_version"])
        return current
    number = db.scalar(
        select(func.coalesce(func.max(WorkflowVersion.version), 0)).where(
            WorkflowVersion.workflow_id == workflow.id
        )
    )
    version = WorkflowVersion(
        workflow=workflow,
        version=number + 1,
        config_hash=digest,
        note=note,
        created_by=user_id,
        nodes=[
            WorkflowNode(
                key=n.key,
                kind=n.kind,
                agent_version_id=n.version_id if n.kind == WorkflowNodeKind.agent else None,
                subworkflow_version_id=(
                    n.version_id if n.kind == WorkflowNodeKind.workflow else None
                ),
                position=i,
            )
            for i, n in enumerate(nodes)
        ],
    )
    db.add(version)
    # Edges reference nodes, and the ORM doesn't know to insert the nodes first.
    db.flush()
    version.edges = [
        WorkflowEdge(source=source, target=target, position=i)
        for i, (source, target) in enumerate(edges)
    ]
    workflow.current_version = version
    workflow.updated_at = func.now()
    return version
