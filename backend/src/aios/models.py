import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataStore(Base):
    """Where a user's data lives: their documents and chunks (see `aios.datastores`).

    It has an id of its own, rather than being the user's, so a store can later change hands
    (to a team) without renaming anything named after it. For now every store is in the platform
    database, and its rows are told apart there by their `owner_id`.
    """

    __tablename__ = "data_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # One store per user, for now. Teams will own stores too (an `owner_team_id`).
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    # Raw upload, kept in Postgres so the API and worker need no shared filesystem/bucket.
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Ingest task deliveries so far; capped so a doc that kills the worker isn't retried forever.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chunk.ordinal",
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    # Raw chunk text (shown to users / the model) and the heading-enriched text that was embedded.
    text: Mapped[str] = mapped_column(Text)
    contextualized: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', contextualized)", persisted=True)
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )


# Agents and skills are split into an editable identity row (name, description) and a chain of
# immutable version rows holding everything that affects model behaviour. Runs, evals and
# traces point at a version, so they always know exactly what the model was given.


class Skill(Base):
    """Reusable instructions that can be attached to agents. Content lives on SkillVersion."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("skill_versions.id", use_alter=True, name="fk_skills_current_version_id")
    )
    # Soft delete: old agent versions pin skill versions, so those must outlive the skill.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["SkillVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among live skills, so a deleted skill's name can be reused.
        Index(
            "uq_skills_owner_id_name_live",
            "owner_id",
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
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", use_alter=True, name="fk_agents_current_version_id")
    )
    # Soft delete: workflow versions pin agent versions, so those must outlive the agent.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        # Names are unique among live agents, so a deleted agent's name can be reused.
        Index(
            "uq_agents_owner_id_name_live",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
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


class AgentRun(Base):
    """One conversation with an agent. Its history is the LangGraph thread `agent-run-<id>`
    (`aios.checkpoints`)."""

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
    # Messages in the thread as of its last turn, for listing runs without loading history.
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    agent: Mapped[Agent] = relationship(back_populates="runs")
    agent_version: Mapped[AgentVersion] = relationship()


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


# Workflows graph agents together, and can nest other workflows as subgraphs. They're versioned
# like agents: an identity row plus immutable versions, and each version pins the exact agent and
# workflow versions its nodes run. A version can only pin versions that already exist, so nesting
# is always finite: no workflow version can contain itself, however deep.


class WorkflowNodeKind(enum.StrEnum):
    agent = "agent"
    workflow = "workflow"


class Workflow(Base):
    """A workflow's identity. Its graph lives on WorkflowVersion."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_versions.id", use_alter=True, name="fk_workflows_current_version_id")
    )
    # Where the editor draws each node, by node key: {"key": {"x": 0, "y": 0}}. Presentation
    # only, like the name, so it isn't versioned; nodes without a position are laid out for you.
    layout: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Soft delete: other workflows' versions pin its versions, so those must outlive it.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["WorkflowVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among live workflows, so a deleted workflow's name can be reused.
        Index(
            "uq_workflows_owner_id_name_live",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class WorkflowVersion(Base):
    """An immutable snapshot of a workflow's graph: its nodes and the edges between them."""

    __tablename__ = "workflow_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    workflow: Mapped[Workflow] = relationship(foreign_keys=[workflow_id])
    nodes: Mapped[list["WorkflowNode"]] = relationship(
        order_by="WorkflowNode.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="WorkflowNode.workflow_version_id",
    )
    edges: Mapped[list["WorkflowEdge"]] = relationship(
        order_by="WorkflowEdge.position", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (UniqueConstraint("workflow_id", "version"),)


class WorkflowNode(Base):
    """A step in a workflow version: a pinned agent version, or a pinned workflow version run as
    a subgraph."""

    __tablename__ = "workflow_nodes"

    workflow_version_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), primary_key=True
    )
    # Names the node within its version; edges refer to nodes by key.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[WorkflowNodeKind] = mapped_column(
        Enum(WorkflowNodeKind, name="workflow_node_kind")
    )
    # Exactly one of these is set, matching `kind`. No ON DELETE: agents and workflows are
    # soft-deleted, so the versions a node pins are never deleted by the API. Only deleting a
    # user removes them, cascading to their agents and workflows alike, so the check is deferred
    # to commit: by then the nodes pinning them have gone too, whatever order the cascades ran in.
    agent_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", deferrable=True, initially="DEFERRED"), index=True
    )
    subworkflow_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_versions.id", deferrable=True, initially="DEFERRED"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)

    agent_version: Mapped[AgentVersion | None] = relationship()
    subworkflow_version: Mapped[WorkflowVersion | None] = relationship(
        foreign_keys=[subworkflow_version_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(kind = 'agent' AND agent_version_id IS NOT NULL AND subworkflow_version_id IS NULL)"
            " OR (kind = 'workflow' AND subworkflow_version_id IS NOT NULL"
            " AND agent_version_id IS NULL)",
            name="ck_workflow_nodes_target",
        ),
    )


class WorkflowEdge(Base):
    """Control passes from `source` to `target` (node keys in the same version) when `source`
    finishes. Nodes with no incoming edge are where the workflow starts."""

    __tablename__ = "workflow_edges"

    workflow_version_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_version_id", "source"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "target"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            ondelete="CASCADE",
        ),
    )


# A workflow run walks its pinned version's graph, recording a step per node as it starts. An
# agent node's conversation is an ordinary agent run, so it opens on the agent's run page.


class WorkflowRunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowStepStatus(enum.StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowRun(Base):
    """One run of a workflow, on the version that was current when it started
    (`aios.workflows.execute_run`)."""

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Owned through the workflow. Soft-deleting it leaves its runs.
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    # Pinned when the run starts: editing the workflow mid-run doesn't change the run.
    workflow_version_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_run_status"), default=WorkflowRunStatus.queued
    )
    # Given to the nodes with no incoming edge.
    input: Mapped[str] = mapped_column(Text)
    # The sink nodes' outputs once it has succeeded; the first error once it has failed.
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Refreshed by the worker while it runs the run, so a run whose worker died can be told from
    # one still going (`aios.workflows.alive`).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow_version: Mapped[WorkflowVersion] = relationship()


class WorkflowRunStep(Base):
    """A node of a run's graph, at any depth, from when it starts."""

    __tablename__ = "workflow_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    # Node keys from the run's workflow down to the node: ["review", "critic"] is node "critic"
    # of the subworkflow at top-level node "review".
    path: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    kind: Mapped[WorkflowNodeKind] = mapped_column(
        Enum(WorkflowNodeKind, name="workflow_node_kind")
    )
    status: Mapped[WorkflowStepStatus] = mapped_column(
        Enum(WorkflowStepStatus, name="workflow_step_status"), default=WorkflowStepStatus.running
    )
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # An agent node's conversation. Deleting the run deletes it (`delete_workflow_run`); if the
    # agent run goes first (deleting its agent), the step stays without it.
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Triggers start agents and workflows without a user in the loop. Each has a kind; the only one
# so far is a schedule (a cron expression in a timezone). Kind-specific settings are columns that
# only that kind sets, so the scheduler can find what's due with one indexed query.


class TriggerKind(enum.StrEnum):
    schedule = "schedule"


class TriggerTargetKind(enum.StrEnum):
    agent = "agent"
    workflow = "workflow"


class TriggerStatus(enum.StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Trigger(Base):
    """Starts an agent (a new run of its current version) or a workflow when it fires."""

    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[TriggerKind] = mapped_column(Enum(TriggerKind, name="trigger_kind"))
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="trigger_target_kind")
    )
    # Exactly one is set, matching `target_kind`. Agents and workflows are soft-deleted, and
    # deleting one deletes its triggers; the cascade is for deleting the user.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    # The message an agent run starts with, or a workflow run's input.
    input: Mapped[str] = mapped_column(Text, server_default="")
    # Schedules: a 5-field cron expression (or a macro like @daily), in an IANA timezone.
    cron: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # When it next fires; null while disabled. Missed runs aren't caught up: a schedule that's
    # overdue (the service was down) fires once, then moves on from now.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[TriggerStatus | None] = mapped_column(
        Enum(TriggerStatus, name="trigger_status")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    # While `last_status` is running, refreshed by the process firing it; one that stops being
    # refreshed has lost its process, and is failed (`aios.scheduler.fail_stalled`).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "agent_runs.id", ondelete="SET NULL", use_alter=True, name="fk_triggers_last_run_id"
        )
    )
    # The workflow run it last started; its status is the schedule's (`api.triggers.trigger_out`),
    # since a worker runs it after the schedule has fired.
    last_workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    agent: Mapped[Agent | None] = relationship()
    workflow: Mapped[Workflow | None] = relationship()
    last_workflow_run: Mapped[WorkflowRun | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_triggers_target",
        ),
        CheckConstraint(
            "kind <> 'schedule' OR (cron IS NOT NULL AND timezone IS NOT NULL)",
            name="ck_triggers_schedule",
        ),
        # What the scheduler polls for.
        Index("ix_triggers_due", "next_run_at", postgresql_where=text("next_run_at IS NOT NULL")),
    )
