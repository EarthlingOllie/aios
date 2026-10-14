import enum
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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
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
