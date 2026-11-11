"""Agents and skills, with their versions, runs and sandbox sessions."""

import enum
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aios.models.base import Base

# Agents and skills are split into an editable identity row (name, description) and a chain of
# immutable version rows holding everything that affects model behaviour. Runs, evals and
# traces point at a version, so they always know exactly what the model was given.


class Skill(Base):
    """Reusable instructions that can be attached to agents. Content lives on SkillVersion."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The organisation it belongs to (`aios.organisations`), and what it answers to: every
    # member's, and gone with the organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("skill_versions.id", use_alter=True, name="fk_skills_current_version_id")
    )
    # Soft delete: old agent versions pin skill versions, so those must outlive the skill.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which member deleted it; null once that user is deleted.
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["SkillVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among the organisation's live skills, so a deleted skill's name
        # can be reused.
        Index(
            "uq_skills_organisation_id_name_live",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class SkillVersion(Base):
    """An immutable snapshot of a skill's content."""

    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    # The skill's name when this version was saved: it's rendered into agent instructions.
    name: Mapped[str] = mapped_column(String(255))
    # Short summary of when to use the skill; `content` holds the instructions themselves.
    description: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    skill: Mapped[Skill] = relationship(foreign_keys=[skill_id])

    __table_args__ = (UniqueConstraint("skill_id", "version"),)


agent_version_skills = Table(
    "agent_version_skills",
    Base.metadata,
    Column(
        "agent_version_id",
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("skill_version_id", ForeignKey("skill_versions.id"), primary_key=True, index=True),
    Column("position", Integer, nullable=False),
)


class Agent(Base):
    """An agent's identity. Its prompt and skills live on AgentVersion."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The organisation it belongs to (`aios.organisations`), and what it answers to: every
    # member's, and gone with the organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", use_alter=True, name="fk_agents_current_version_id")
    )
    # Which of the app's built-in agents it is, if it's one: made for its organisation by the app
    # rather than by a member (`aios.assistant`), and found again by this. Null for the members'
    # own agents.
    builtin: Mapped[str | None] = mapped_column(String(32))
    # Soft delete: workflow versions pin agent versions, so those must outlive the agent.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which member deleted it; null once that user is deleted.
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["AgentVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        # Names are unique among the organisation's live agents, so a deleted agent's name
        # can be reused.
        Index(
            "uq_agents_organisation_id_name_live",
            "organisation_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # One live agent of each built-in kind per organisation.
        Index(
            "uq_agents_organisation_id_builtin_live",
            "organisation_id",
            "builtin",
            unique=True,
            postgresql_where=text("builtin IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


class AgentVersion(Base):
    """An immutable snapshot of everything that shapes an agent's behaviour."""

    __tablename__ = "agent_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    base_prompt: Mapped[str] = mapped_column(Text)
    # LangChain "provider:model" name; null means the deployment default (settings.llm_model).
    model: Mapped[str | None] = mapped_column(String(255))
    # The exact instructions sent to the model, rendered once when the version is created.
    instructions: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped[Agent] = relationship(foreign_keys=[agent_id])
    # View-only: rows carry a position, so versioning.save_agent_version inserts them directly.
    skills: Mapped[list[SkillVersion]] = relationship(
        secondary=agent_version_skills, order_by=agent_version_skills.c.position, viewonly=True
    )
    tools: Mapped[list["AgentVersionTool"]] = relationship(
        order_by="AgentVersionTool.position", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (UniqueConstraint("agent_id", "version"),)


class AgentVersionTool(Base):
    """A tool from the library (`aios.tools`) given to an agent version, with its settings."""

    __tablename__ = "agent_version_tools"

    agent_version_id: Mapped[int] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="CASCADE"), primary_key=True
    )
    # A key in aios.tools.TOOLS. Tools are code, so there's no row to reference.
    tool_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The tool's validated settings (its config_model, defaults filled in).
    config: Mapped[dict] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer)


class AgentRunStatus(enum.StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class AgentRun(Base):
    """One conversation with an agent. Its history is the LangGraph thread `agent-run-<id>`
    (`aios.checkpoints`).

    How it went and what it cost are written by its turns (`aios.api.agents.turn_stream`): the
    status and times are its latest turn's, the counters add up over all of them. Null for runs
    from before they were kept, which are shown as not tracked rather than as zero.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    # Pinned when the run starts: editing the agent mid-conversation doesn't change the run.
    agent_version_id: Mapped[int] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    # The trigger that started the run, if one did (and still exists).
    trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL"), index=True
    )
    # The member who started it; null for a schedule's run, or once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Messages in the thread as of its last turn, for listing runs without loading history.
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Running while a turn of it is in progress, then how that turn ended.
    status: Mapped[AgentRunStatus | None] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status")
    )
    # When its latest turn began and ended.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The model's tool calls, and the tokens of each model call, summed: input tokens count the
    # context again at each step, as bills do.
    tool_call_count: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    # While a turn runs outside a request (`aios.firing`), refreshed by the process running it;
    # a running run whose heartbeat has stopped has lost its process, and is failed
    # (`aios.firing.fail_stalled`). Null for a turn bound to a request, which ends with it.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    agent: Mapped[Agent] = relationship(back_populates="runs")
    agent_version: Mapped[AgentVersion] = relationship()

    __table_args__ = (
        # What the stall sweep looks at.
        Index(
            "ix_agent_runs_running_heartbeat",
            "heartbeat_at",
            postgresql_where=text("status = 'running' AND heartbeat_at IS NOT NULL"),
        ),
    )


class SandboxSession(Base):
    """The sandbox an agent run's `run_python` calls go to (see `aios.sandbox`)."""

    __tablename__ = "sandbox_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), unique=True
    )
    # The backend's id for the sandbox (a task ARN, a broker session id).
    ref: Mapped[str] = mapped_column(String(512))
    # Where exec calls go, and the bearer token they need.
    endpoint: Mapped[str] = mapped_column(String(512))
    secret: Mapped[str] = mapped_column(String(255))
    # This sandbox replaced one that had gone, and no call has run since: the next call's result
    # tells the model that /work was reset.
    replaced: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
