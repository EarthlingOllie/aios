"""The tables every data store has, in its `aios` schema, and the run history export's, in its
`history` schema once its organisation first switches the export on (the store tree, `migrations/`,
makes them). Sessions on them come from `store_session`, on the store's own database: they're never
in the platform database (`aios.models`), and a store's rows are all its own, so nothing here
says whose they are.
"""

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from aios.models.workflows import WorkflowNodeKind

EMBEDDING_DIM = 1024

SCHEMA = "aios"
# The run history export's tables (`aios.datastores.history`).
HISTORY_SCHEMA = "history"
# The exported messages worth embedding, and searching: what the user and the assistant said
# (not tool results, which are the tools' own, nor the empty text of a turn of only tool calls).
EMBEDDABLE = "role IN ('user', 'assistant') AND btrim(text) <> ''"


class StoreBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class SchemaHistory(StoreBase):
    """The log of changes to the store's schema: approved schema change requests append to it
    as they're applied."""

    __tablename__ = "schema_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The schema change request it applied (in the platform database), if any.
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(Text)
    # The statements that ran.
    sql: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmbeddingColumn(StoreBase):
    """A `vector(EMBEDDING_DIM)` column of a table in `data`, and the text column in the same
    table it's the embedding of: `write_data` fills it. Schema changes add a row as they add
    such a column; only the owner role writes it (`provision.BOOKKEEPING`)."""

    __tablename__ = "embedding_columns"
    __table_args__ = {
        "comment": "Columns in data that write_data fills with embeddings of a text column"
    }

    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    column_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_column: Mapped[str] = mapped_column(Text)


class DroppedObject(StoreBase):
    """A table or column in `data` that an approved drop renamed out of the way, until it's
    restored or purged (`aios.datastores.dropped`). Only the owner role writes it
    (`provision.BOOKKEEPING`)."""

    __tablename__ = "dropped_objects"
    __table_args__ = {
        "comment": "Tables and columns in data that were dropped, until restored or purged"
    }

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # A dropped table: its name before it was dropped. A dropped column: the table it's in, as
    # that's named now (a dropped table's new name, if the table was dropped after it).
    table_name: Mapped[str] = mapped_column(Text)
    # A dropped column: its name before it was dropped. None for a table.
    column_name: Mapped[str | None] = mapped_column(Text)
    # What it was renamed to.
    dropped_name: Mapped[str] = mapped_column(Text)
    # A column that was NOT NULL (dropping it lifts that, so new rows can leave it out).
    not_null: Mapped[bool] = mapped_column(Boolean, server_default=false())
    dropped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Why the last restore a member asked for was refused, if it was.
    restore_error: Mapped[str | None] = mapped_column(Text)


class Person(StoreBase):
    """The people mirror: one row per person who is or was a member of the store's organisation,
    as the platform (the source of truth) has them (`aios.datastores.people`). The platform
    writes it after its own commits, and the repair sweep rebuilds it from memberships. Their
    email is on the deny list: the read-only role (and so `query_data`, `describe_data` and the
    Tables view) may read every column but it (`provision.DENIED_COLUMNS`)."""

    __tablename__ = "people"
    __table_args__ = {
        "comment": "The organisation's people, current and former members, with their roles"
    }

    # Their platform user id (a store has no users table, so no foreign key).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    # admin, editor or participant.
    role: Mapped[str] = mapped_column(Text)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When they left; null while they're a member.
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Agents, skills and workflows: what the organisation builds. Each is an editable identity row
# (its name and description) and a chain of immutable version rows holding everything that
# affects model behaviour (`aios.versioning`); runs, evals and traces point at a version, so they
# always know exactly what the model was given. None is ever deleted, only archived: older
# versions pin their versions, and their runs are the members' history with them. Who made,
# changed, archived or saved each is a platform user id, resolved through the people mirror
# (`aios.api.provenance`); a store has no users table, so there's no foreign key. Ids are the
# store's own, and repeat across organisations.


class Skill(StoreBase):
    """Reusable instructions that can be attached to agents. Content lives on SkillVersion."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which member made it and which last changed it.
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("skill_versions.id", use_alter=True, name="fk_skills_current_version_id")
    )
    # When it was archived, and by which member; both null while it isn't. Archived rather
    # than deleted: old agent versions pin its versions.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["SkillVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among the skills, archived ones included: an archived skill's name
        # stays reserved.
        Index("uq_skills_name", "name", unique=True),
        {"comment": "Skills: reusable instructions agents can be given"},
    )


class SkillVersion(StoreBase):
    """An immutable snapshot of a skill's content."""

    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    version: Mapped[int] = mapped_column(Integer)
    # The skill's name when this version was saved: it's rendered into agent instructions.
    name: Mapped[str] = mapped_column(String(255))
    # Short summary of when to use the skill; `content` holds the instructions themselves.
    description: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    skill: Mapped[Skill] = relationship(foreign_keys=[skill_id])

    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_id_version"),
        {"comment": "Each skill's versions, which never change once saved"},
    )


agent_version_skills = Table(
    "agent_version_skills",
    StoreBase.metadata,
    Column("agent_version_id", ForeignKey("agent_versions.id"), primary_key=True),
    Column("skill_version_id", ForeignKey("skill_versions.id"), primary_key=True),
    Column("position", Integer, nullable=False),
    Index("ix_agent_version_skills_skill_version_id", "skill_version_id"),
    comment="The skill versions each agent version pins, in order",
)


class Agent(StoreBase):
    """An agent's identity. Its prompt and skills live on AgentVersion."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which member made it and which last changed it.
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", use_alter=True, name="fk_agents_current_version_id")
    )
    # Which of the app's built-in agents it is, if it's one: made by the app rather than by a
    # member (`aios.assistant`), and found again by this. Null for the members' own agents.
    builtin: Mapped[str | None] = mapped_column(String(32))
    # When it was archived, and by which member; both null while it isn't. Archived rather
    # than deleted: workflow versions pin its versions, and its runs are the members' history
    # with it. An archived agent starts nothing new.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["AgentVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among the agents, archived ones included: an archived agent's name
        # stays reserved, so unarchiving it never clashes.
        Index("uq_agents_name", "name", unique=True),
        # One agent of each built-in kind.
        Index(
            "uq_agents_builtin",
            "builtin",
            unique=True,
            postgresql_where=sql_text("builtin IS NOT NULL"),
        ),
        {"comment": "Agents: each a name, and its current version"},
    )


class AgentVersion(StoreBase):
    """An immutable snapshot of everything that shapes an agent's behaviour."""

    __tablename__ = "agent_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    version: Mapped[int] = mapped_column(Integer)
    base_prompt: Mapped[str] = mapped_column(Text)
    # LangChain "provider:model" name; null means the deployment default (settings.llm_model).
    model: Mapped[str | None] = mapped_column(String(255))
    # The exact instructions sent to the model, rendered once when the version is created.
    instructions: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped[Agent] = relationship(foreign_keys=[agent_id])
    # View-only: rows carry a position, so versioning.save_agent_version inserts them directly.
    skills: Mapped[list[SkillVersion]] = relationship(
        secondary=agent_version_skills, order_by=agent_version_skills.c.position, viewonly=True
    )
    tools: Mapped[list["AgentVersionTool"]] = relationship(
        order_by="AgentVersionTool.position", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),
        {"comment": "Each agent's versions, which never change once saved"},
    )


class AgentVersionTool(StoreBase):
    """A tool from the library (`aios.tools`) given to an agent version, with its settings."""

    __tablename__ = "agent_version_tools"
    __table_args__ = {"comment": "The tools each agent version has, in order, with their settings"}

    agent_version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id"), primary_key=True)
    # A key in aios.tools.TOOLS. Tools are code, so there's no row to reference.
    tool_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The tool's validated settings (its config_model, defaults filled in).
    config: Mapped[dict] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer)


# Workflows graph agents together, and can nest other workflows as subgraphs. They're versioned
# like agents: an identity row plus immutable versions, and each version pins the exact agent and
# workflow versions its nodes run. A version can only pin versions that already exist, so nesting
# is always finite: no workflow version can contain itself, however deep.


class Workflow(StoreBase):
    """A workflow's identity. Its graph lives on WorkflowVersion."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which member made it and which last changed it.
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_versions.id", use_alter=True, name="fk_workflows_current_version_id")
    )
    # Where the editor draws each node, by node key: {"key": {"x": 0, "y": 0}}. Presentation
    # only, like the name, so it isn't versioned; nodes without a position are laid out for you.
    layout: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    # When it was archived, and by which member; both null while it isn't. Archived rather
    # than deleted: other workflows' versions pin its versions.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["WorkflowVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among the workflows, archived ones included: an archived workflow's
        # name stays reserved.
        Index("uq_workflows_name", "name", unique=True),
        {"comment": "Workflows: each a name, its current version and its layout"},
    )


class WorkflowVersion(StoreBase):
    """An immutable snapshot of a workflow's graph: its nodes and the edges between them."""

    __tablename__ = "workflow_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    version: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer)
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

    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_id_version"),
        {"comment": "Each workflow's versions, which never change once saved"},
    )


class WorkflowNode(StoreBase):
    """A step in a workflow version: a pinned agent version, or a pinned workflow version run as
    a subgraph."""

    __tablename__ = "workflow_nodes"

    workflow_version_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_versions.id"), primary_key=True
    )
    # Names the node within its version; edges refer to nodes by key.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[WorkflowNodeKind] = mapped_column(
        Enum(WorkflowNodeKind, name="workflow_node_kind")
    )
    # Exactly one of these is set, matching `kind`. Agents and workflows are archived, never
    # deleted, so the versions a node pins are always there.
    agent_version_id: Mapped[int | None] = mapped_column(ForeignKey("agent_versions.id"))
    subworkflow_version_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_versions.id"))
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
        Index("ix_workflow_nodes_agent_version_id", "agent_version_id"),
        Index("ix_workflow_nodes_subworkflow_version_id", "subworkflow_version_id"),
        {"comment": "Each workflow version's nodes: an agent version or a workflow version"},
    )


class WorkflowEdge(StoreBase):
    """Control passes from `source` to `target` (node keys in the same version) when `source`
    finishes. Nodes with no incoming edge are where the workflow starts."""

    __tablename__ = "workflow_edges"

    workflow_version_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_versions.id"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_version_id", "source"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            name="fk_workflow_edges_source",
        ),
        ForeignKeyConstraint(
            ["workflow_version_id", "target"],
            ["workflow_nodes.workflow_version_id", "workflow_nodes.key"],
            name="fk_workflow_edges_target",
        ),
        {"comment": "Each workflow version's edges: control passes from source to target"},
    )


class DocumentStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class Document(StoreBase):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    # Where the upload is, in S3 (`aios.datastores.files.document_key`), its size and its
    # SHA-256 (hex).
    s3_key: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Ingest task deliveries so far; capped so a doc that kills the worker isn't retried forever.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    # The member who uploaded it, by platform user id (a store has no users table, so no
    # foreign key); null for documents from before it was recorded.
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chunk.ordinal",
    )


class Chunk(StoreBase):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
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
        Index("ix_chunks_document_id", "document_id"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )


# The run history export: a copy of the organisation's agent and workflow runs, keyed by the
# platform's ids. The platform database stays the source of truth; these are only ever written by
# the export.


class HistoryAgentRun(StoreBase):
    __tablename__ = "agent_runs"
    __table_args__ = {
        "schema": HISTORY_SCHEMA,
        "comment": "A copy of an agent run (one conversation with an agent), from the platform.",
    }

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The run's id on the platform."
    )
    agent_id: Mapped[int] = mapped_column(BigInteger)
    agent_name: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[int] = mapped_column(Integer, comment="The agent version it ran.")
    model: Mapped[str | None] = mapped_column(
        Text, comment="The version's model; null means the deployment's default."
    )
    title: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The member who started it; null for a schedule's run."
    )
    trigger_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The schedule that started it, if one did."
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The workflow run whose agent node it was, if any."
    )
    message_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="When its last turn ended."
    )
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoryMessage(StoreBase):
    __tablename__ = "messages"

    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey(f"{HISTORY_SCHEMA}.agent_runs.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer, comment="Its place in the run, from 0.")
    message_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, comment="user, assistant, tool or system.")
    text: Mapped[str] = mapped_column(Text, comment="Its text, without tool calls.")
    tool_calls: Mapped[list | None] = mapped_column(
        JSONB, comment="An assistant message's tool calls: [{id, name, args}]."
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        Text, comment="A tool message's call, in its run's tool_calls."
    )
    tool_name: Mapped[str | None] = mapped_column(Text)
    is_error: Mapped[bool | None] = mapped_column(Boolean, comment="A tool message that failed.")
    usage: Mapped[dict | None] = mapped_column(
        JSONB, comment="An assistant message's token usage: {input_tokens, output_tokens, ...}."
    )
    model: Mapped[str | None] = mapped_column(Text, comment="The model that wrote it.")
    # For searching past runs (`aios.search`): the text's embedding, while the organisation
    # has embedding on, and its full-text vector, always.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM),
        comment="Its text's embedding, for search: a user's or assistant's message with text, "
        "once embedded (only while embedding is on).",
    )
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        comment="Its text's full-text search vector.",
    )

    __table_args__ = (
        PrimaryKeyConstraint("agent_run_id", "position"),
        Index(
            "ix_messages_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
        # The messages waiting to be embedded (`EMBEDDABLE`), for the embedding to find.
        Index(
            "ix_messages_unembedded",
            "agent_run_id",
            "position",
            postgresql_where=sql_text(f"embedding IS NULL AND {EMBEDDABLE}"),
        ),
        {
            "schema": HISTORY_SCHEMA,
            "comment": "The messages of the agent runs in history.agent_runs, in order.",
        },
    )


class HistoryWorkflowRun(StoreBase):
    __tablename__ = "workflow_runs"
    __table_args__ = {
        "schema": HISTORY_SCHEMA,
        "comment": "A copy of a workflow run, from the platform.",
    }

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The run's id on the platform."
    )
    workflow_id: Mapped[int] = mapped_column(BigInteger)
    workflow_name: Mapped[str] = mapped_column(Text)
    workflow_version: Mapped[int] = mapped_column(Integer, comment="The workflow version it ran.")
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The member who started it; null for a schedule's run."
    )
    # Text, not an enum: a status the platform adds later is copied as it is.
    status: Mapped[str] = mapped_column(
        Text, comment="queued, running, succeeded or failed, as on the platform when exported."
    )
    input: Mapped[str] = mapped_column(Text, comment="What it was given.")
    output: Mapped[str | None] = mapped_column(Text, comment="Its final nodes' outputs, once done.")
    error: Mapped[str | None] = mapped_column(Text, comment="Why it failed.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoryWorkflowRunStep(StoreBase):
    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        Index("ix_workflow_run_steps_workflow_run_id", "workflow_run_id"),
        {
            "schema": HISTORY_SCHEMA,
            "comment": "The steps of the workflow runs in history.workflow_runs: a node, at any "
            "depth.",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The step's id on the platform."
    )
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey(f"{HISTORY_SCHEMA}.workflow_runs.id", ondelete="CASCADE")
    )
    path: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        comment="Node keys from the run's workflow down to the node: {review,critic} is "
        "node critic of the subworkflow at node review.",
    )
    kind: Mapped[str] = mapped_column(Text, comment="agent or workflow.")
    status: Mapped[str] = mapped_column(
        Text, comment="running, succeeded or failed, as on the platform when exported."
    )
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # Not a foreign key: agent runs and workflow runs are exported apart, in either order.
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="An agent node's conversation: history.agent_runs.id."
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
