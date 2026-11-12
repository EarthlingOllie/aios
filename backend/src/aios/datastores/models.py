"""The tables every data store has, in its `aios` schema (the store tree, `migrations/`, makes
them). Sessions on them come from `store_session`, on the store's own database: they're never
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
    Double,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
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

from aios.jev import Verdict
from aios.models.agents import AgentRunStatus
from aios.models.approvals import SchemaChangeStatus
from aios.models.audit import AuditActorKind
from aios.models.chats import (
    ChatAuthorKind,
    ChatMemberKind,
    ChatMessageKind,
    InvocationCause,
    InvocationStatus,
)
from aios.models.triggers import EventType, TriggerKind, TriggerStatus, TriggerTargetKind
from aios.models.usage import InvocableBy, UsageKind, UsagePurpose
from aios.models.workflows import WorkflowNodeKind, WorkflowRunStatus, WorkflowStepStatus

EMBEDDING_DIM = 1024

SCHEMA = "aios"


class StoreBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class SchemaHistory(StoreBase):
    """The log of changes to the store's schema: approved schema change requests append to it
    as they're applied."""

    __tablename__ = "schema_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The schema change request it applied (`schema_change_requests`), if any. Not a foreign
    # key: the log outlives what it records.
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
    # Who may invoke it by mentioning it (`aios.guards`), and its own caps (`aios.limits`):
    # only those it lowers, or an admin raised, by name; the rest follow the organisation's.
    invocable_by: Mapped[InvocableBy] = mapped_column(
        Enum(InvocableBy, name="invocable_by"),
        default=InvocableBy.everyone,
        server_default=InvocableBy.everyone.value,
    )
    limits: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
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
    # Who may invoke it by mentioning it (`aios.guards`), and its own caps (`aios.limits`):
    # only those it lowers, or an admin raised, by name; the rest follow the organisation's.
    invocable_by: Mapped[InvocableBy] = mapped_column(
        Enum(InvocableBy, name="invocable_by"),
        default=InvocableBy.everyone,
        server_default=InvocableBy.everyone.value,
    )
    limits: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
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


# Runs: the organisation's agent runs, with their sandboxes, and workflow runs, with their steps.
# A run's conversation is its checkpointer thread (`aios.checkpoints`), in the same store.


class AgentRun(StoreBase):
    """One conversation with an agent. Its history is the checkpointer thread `agent-run-<id>`,
    in this store (`aios.checkpoints`).

    How it went and what it cost are its turns' (`Turn`, `aios.turns`): its status and times
    are its latest turn's, its counters add up over all of them. Why it ran is its invocation
    (`Invocation.agent_run_id`).
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    # Pinned when the run starts: editing the agent mid-conversation doesn't change the run.
    agent_version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id"))
    title: Mapped[str | None] = mapped_column(String(255))
    # The schedule that started the run, if one did; cleared when it's deleted.
    trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL", name="fk_agent_runs_trigger_id")
    )
    # The member who started it (a platform user id, resolved through the people mirror); null
    # for a schedule's run.
    created_by: Mapped[int | None] = mapped_column(Integer)
    # Messages in the thread as of its last turn, for listing runs without loading history.
    message_count: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    # No longer set: turns outside a request run on the run service (`aios.run_service`), whose
    # leases and retries replace it. Cleared by a request's turn; goes with the old heartbeat
    # columns.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The turn lease (`aios.turn_lease`): the token of the turn that holds the run, and when its
    # hold lapses unless refreshed. Both null while no turn holds it; an expired one is free.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_agent_runs_agent_id", "agent_id"),
        Index("ix_agent_runs_agent_version_id", "agent_version_id"),
        # What the stall sweep looks at.
        Index(
            "ix_agent_runs_heartbeat",
            "heartbeat_at",
            postgresql_where=sql_text("heartbeat_at IS NOT NULL"),
        ),
        {"comment": "Conversations with agents: each run's history is its checkpointer thread"},
    )


class Turn(StoreBase):
    """One turn of an agent run (`aios.turns`): from a message (or a resume) to the agent's
    reply, on the version the run ran on, for the invocation the run ran for."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    # Why it ran: its run's invocation.
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invocations.id", ondelete="SET NULL")
    )
    agent_version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id"))
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="turn_status"), default=AgentRunStatus.running
    )
    # Why it failed.
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Its model calls; their tool calls (the provider's own too, e.g. web search); and their
    # tokens, summed: input tokens count the context again at each call, as bills do.
    model_calls: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    tool_calls: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))

    __table_args__ = (
        Index("ix_turns_agent_run_id_id", "agent_run_id", "id"),
        Index("ix_turns_invocation_id", "invocation_id"),
        Index("ix_turns_agent_version_id", "agent_version_id"),
        {"comment": "Each turn of an agent run: what it ran on, how it went, and what it cost"},
    )


class SandboxSession(StoreBase):
    """The sandbox an agent run's `run_python` calls go to (see `aios.sandbox`)."""

    __tablename__ = "sandbox_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), unique=True
    )
    # The backend's id for the sandbox (a task ARN, a broker session id).
    ref: Mapped[str] = mapped_column(String(512))
    # Where exec calls go, and the bearer token they need (on the deny list,
    # `aios.datastores.provision.DENIED_COLUMNS`).
    endpoint: Mapped[str] = mapped_column(String(512))
    secret: Mapped[str] = mapped_column(String(255))
    # This sandbox replaced one that had gone, and no call has run since: the next call's result
    # tells the model that /work was reset.
    replaced: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = ({"comment": "The sandbox each agent run's Python runs in"},)


class WorkflowRun(StoreBase):
    """One run of a workflow, on the version that was current when it started, advanced node by
    node on the run service (`aios.workflows.advance`)."""

    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id"))
    # Pinned when the run starts: editing the workflow mid-run doesn't change the run.
    workflow_version_id: Mapped[int] = mapped_column(ForeignKey("workflow_versions.id"))
    # The member who started it (a platform user id); null for a schedule's run.
    created_by: Mapped[int | None] = mapped_column(Integer)
    # The schedule that started it, if one did; cleared when it's deleted.
    trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey("triggers.id", ondelete="SET NULL", name="fk_workflow_runs_trigger_id")
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
    # No longer written: runs on the run service hold no process, so beat no heartbeat. Dropped
    # with the other heartbeat columns.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # While it's waiting, since when; and how long it has waited in all before that. Waiting
    # doesn't count towards the run's time limit.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waited_seconds: Mapped[float] = mapped_column(Double, server_default=sql_text("0"))

    __table_args__ = (
        Index("ix_workflow_runs_workflow_id", "workflow_id"),
        Index("ix_workflow_runs_workflow_version_id", "workflow_version_id"),
        {"comment": "Runs of workflows: each walks its pinned version's graph"},
    )


class WorkflowRunStep(StoreBase):
    """A node of a run's graph, at any depth, from when it starts."""

    __tablename__ = "workflow_run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
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
    # An agent node's conversation. Deleting the workflow run deletes it (`delete_workflow_run`).
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    # While an agent node waits: the schema change request it waits on.
    waiting_on_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "schema_change_requests.id",
            ondelete="SET NULL",
            name="fk_workflow_run_steps_waiting_on_request_id",
        )
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_workflow_run_steps_run_id", "run_id"),
        Index("ix_workflow_run_steps_agent_run_id", "agent_run_id"),
        Index("ix_workflow_run_steps_waiting_on_request_id", "waiting_on_request_id"),
        {"comment": "Each workflow run's steps: a node of its graph, at any depth"},
    )


# Schedules, chats, invocations and schema change requests: what starts runs, and what the
# organisation's people say to each other and to its agents and workflows. Who made, posted or
# asked for each is a platform user id, resolved through the people mirror (no foreign key).


class Trigger(StoreBase):
    """Starts an agent (a new run of its current version) or a workflow when it fires. A
    schedule is a cron expression in a timezone: the platform keeps a thin index of when each
    is next due (`aios.models.ScheduleDue`, `aios.schedules`), written whenever one is saved and
    rebuilt from these by the repair sweep. An event trigger fires when the store's outbox has
    a matching event (`aios.events`)."""

    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which member made it and which last changed it.
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[TriggerKind] = mapped_column(Enum(TriggerKind, name="trigger_kind"))
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="trigger_target_kind")
    )
    # Exactly one is set, matching `target_kind`. They're archived, never deleted.
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    # The message an agent run starts with, or a workflow run's input.
    input: Mapped[str] = mapped_column(Text, server_default="")
    # Schedules: a 5-field cron expression (or a macro like @daily), in an IANA timezone.
    cron: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Event triggers: the event type it fires on, and what the event must match (by filter
    # field, `aios.events.FILTERS`).
    event_type: Mapped[EventType | None] = mapped_column(Enum(EventType, name="event_type"))
    filter: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    # Whether it may fire on events its own target caused (the loop rules, `aios.events`).
    fire_on_own_events: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    # Why it was turned off, when the platform did it.
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    # When it next fires; null while disabled. Missed runs aren't caught up: a schedule that's
    # overdue (the service was down) fires once, then moves on from now.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[TriggerStatus | None] = mapped_column(
        Enum(TriggerStatus, name="trigger_status")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    # When it was claimed, while `last_status` is running: one claimed long ago that never
    # started its run lost its process, and is failed (`aios.scheduler.fail_stalled`).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The agent run, or workflow run, it last started; cleared when that run is deleted. A
    # workflow run's status is the schedule's (`api.triggers.trigger_out`), since a worker runs
    # it after the schedule has fired.
    last_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "agent_runs.id", ondelete="SET NULL", use_alter=True, name="fk_triggers_last_run_id"
        )
    )
    last_workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "workflow_runs.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_triggers_last_workflow_run_id",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

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
        CheckConstraint(
            "kind::text <> 'event' OR event_type IS NOT NULL", name="ck_triggers_event"
        ),
        Index(
            "ix_triggers_event_type",
            "event_type",
            postgresql_where=sql_text("event_type IS NOT NULL"),
        ),
        Index("ix_triggers_agent_id", "agent_id"),
        Index("ix_triggers_workflow_id", "workflow_id"),
        # What the stall sweep looks at.
        Index(
            "ix_triggers_running",
            "heartbeat_at",
            postgresql_where=sql_text("last_status = 'running'"),
        ),
        {"comment": "Schedules: what starts the organisation's agents and workflows by itself"},
    )


# Chats are open conversations between people, agents and workflows: anyone in the organisation
# reads any chat, and posting joins. A member row says who is in a chat; which kind it is decides
# which id column is set, like a trigger's target.


class Chat(StoreBase):
    """A group conversation. Its members are `ChatMember` rows, its messages `ChatMessage`s."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Which member made it and which last changed it.
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When it was last active: when it was last changed, or a message was last posted.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list["ChatMember"]] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMember.id",
    )

    __table_args__ = (
        # Named like a channel: unique among the organisation's chats.
        Index("uq_chats_name", "name", unique=True),
        {"comment": "Chats: group conversations between people, agents and workflows"},
    )


class ChatMember(StoreBase):
    """Who is in a chat: a person, an agent or a workflow of the organisation."""

    __tablename__ = "chat_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    kind: Mapped[ChatMemberKind] = mapped_column(Enum(ChatMemberKind, name="chat_member_kind"))
    # Exactly one is set, matching `kind`: a person (a platform user id), or an agent or
    # workflow, which are archived, never deleted.
    user_id: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"))
    added_by: Mapped[int | None] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # People only: the id of the last of the chat's messages they've seen (null: none yet), from
    # which their unread count is counted. A cursor, not a reference: it only goes up.
    last_read_message_id: Mapped[int | None] = mapped_column(Integer)

    chat: Mapped[Chat] = relationship(back_populates="members")

    __table_args__ = (
        CheckConstraint(
            "(kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL AND workflow_id IS NULL)"
            " OR (kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL"
            " AND workflow_id IS NULL)"
            " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND user_id IS NULL"
            " AND agent_id IS NULL)",
            name="ck_chat_members_target",
        ),
        UniqueConstraint("chat_id", "user_id", name="uq_chat_members_chat_id_user_id"),
        UniqueConstraint("chat_id", "agent_id", name="uq_chat_members_chat_id_agent_id"),
        UniqueConstraint("chat_id", "workflow_id", name="uq_chat_members_chat_id_workflow_id"),
        Index("ix_chat_members_user_id", "user_id"),
        Index("ix_chat_members_agent_id", "agent_id"),
        Index("ix_chat_members_workflow_id", "workflow_id"),
        {"comment": "Who is in each chat: people, agents and workflows"},
    )


class ChatMessage(StoreBase):
    """A message in a chat, read in id order: `?after=` and `?before=` page by id."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    author_kind: Mapped[ChatAuthorKind] = mapped_column(
        Enum(ChatAuthorKind, name="chat_author_kind")
    )
    # At most one is set, matching `author_kind`; none for a system message.
    author_user_id: Mapped[int | None] = mapped_column(Integer)
    author_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    author_workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"))
    kind: Mapped[ChatMessageKind] = mapped_column(Enum(ChatMessageKind, name="chat_message_kind"))
    body: Mapped[str] = mapped_column(Text)
    # Who the message mentions, `[{kind, id, name}]`, with the names as they were when posted.
    mentions: Mapped[list] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    # Jev's verdict (`aios.jev.Verdict`): a person's message is checked as it's posted, and
    # stored only if clean; what agents, workflows and the chat post is derived, never checked.
    # Copied onto its search item.
    verdict: Mapped[Verdict] = mapped_column(
        Enum(Verdict, name="verdict"),
        server_default=Verdict.derived.value,
        default=Verdict.derived,
        comment="Jev's verdict: clean for a person's message (checked as it's posted),"
        " derived for what agents, workflows and the chat post.",
    )
    # The invocation it's part of: a run card's, or the result the run posted back.
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "invocations.id",
            ondelete="CASCADE",
            use_alter=True,
            name="fk_chat_messages_invocation_id",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(author_kind = 'user' OR author_user_id IS NULL)"
            " AND (author_kind = 'agent' OR author_agent_id IS NULL)"
            " AND (author_kind = 'workflow' OR author_workflow_id IS NULL)",
            name="ck_chat_messages_author",
        ),
        CheckConstraint(
            "kind <> 'run_card' OR invocation_id IS NOT NULL", name="ck_chat_messages_run_card"
        ),
        # A chat's messages in order, for the cursor reads.
        Index("ix_chat_messages_chat_id_id", "chat_id", "id"),
        Index("ix_chat_messages_invocation_id", "invocation_id"),
        Index("ix_chat_messages_author_user_id", "author_user_id"),
        {"comment": "Each chat's messages, in id order"},
    )


class Invocation(StoreBase):
    """Why a run of an agent or workflow ran (`aios.invocations`): a mention in a chat, a
    schedule, or a person starting it by hand. Every run has exactly one. A workflow agent
    node's is a child of its workflow run's. A mention's card follows the run; when the run
    ends, its result is posted back and it finishes."""

    __tablename__ = "invocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    cause: Mapped[InvocationCause] = mapped_column(
        Enum(InvocationCause, name="invocation_cause"),
        default=InvocationCause.mention,
        server_default=InvocationCause.mention.value,
    )
    # Where from: a mention's chat, and the message that mentioned it.
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    # Who or what caused it: the member who mentioned it or started its run by hand (its run's
    # `created_by`); the schedule that fired it (cleared if it's deleted); the invocation it's a
    # part of (a workflow agent node's: its workflow run's).
    requested_by: Mapped[int | None] = mapped_column(Integer)
    trigger_id: Mapped[int | None] = mapped_column(ForeignKey("triggers.id", ondelete="SET NULL"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("invocations.id", ondelete="SET NULL"))
    # An event trigger's: the event that fired it (cleared once the event is pruned).
    event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "events.id", ondelete="SET NULL", use_alter=True, name="fk_invocations_event_id"
        ),
    )
    # Loop prevention: its ancestors' ids, the root first, and how many there are.
    ancestor_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default=sql_text("'{}'::integer[]")
    )
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"))
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="trigger_target_kind")
    )
    # Exactly one is set, matching `target_kind`.
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id"))
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id"))
    status: Mapped[InvocationStatus] = mapped_column(
        Enum(InvocationStatus, name="invocation_status"), default=InvocationStatus.queued
    )
    # Why it failed: the run's error, or why it couldn't start.
    error: Mapped[str | None] = mapped_column(Text)
    # Why it was refused (`aios.guards`), for a refused one.
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    # What the handoff told its run of the chat (`aios.handoff`); null until it has.
    brief: Mapped[str | None] = mapped_column(Text)
    # The run it started; null until it has, and once that run has been deleted (the card says
    # so). Deleting the chat leaves the run.
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When its run started (null if it never did), and when it finished.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # No longer set: an agent invocation's turn runs on the run service (`aios.run_service`),
    # whose leases and retries replace it. Goes with the old heartbeat columns.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_invocations_target",
        ),
        CheckConstraint(
            "cause <> 'mention' OR depth > 0 OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
            name="ck_invocations_mention",
        ),
        CheckConstraint("depth = cardinality(ancestor_ids)", name="ck_invocations_depth"),
        Index("ix_invocations_chat_id", "chat_id"),
        Index("ix_invocations_message_id", "message_id"),
        Index("ix_invocations_agent_run_id", "agent_run_id"),
        Index("ix_invocations_workflow_run_id", "workflow_run_id"),
        Index("ix_invocations_trigger_id", "trigger_id"),
        Index("ix_invocations_parent_id", "parent_id"),
        Index("ix_invocations_event_id", "event_id"),
        # The unfinished ones: what a chat's poll reports on, and what waits for a turn.
        Index(
            "ix_invocations_unfinished",
            "chat_id",
            postgresql_where=sql_text("status IN ('queued', 'briefing', 'running')"),
        ),
        {"comment": "Why each run ran: a mention, a schedule, or a person starting it by hand"},
    )


class Event(StoreBase):
    """The outbox (`aios.events`): something that happened in the store, written in the same
    transaction as the change, for event triggers to react to. Dispatched ones are pruned after
    `event_retention_days`."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[EventType] = mapped_column(Enum(EventType, name="event_type"))
    payload: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    # The invocation whose run caused it; null for a person's own action.
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invocations.id", ondelete="SET NULL")
    )
    # Its chain of causes, root first: the causing invocation's ancestors, then it; empty for a
    # person's own action.
    chain: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default=sql_text("'{}'::integer[]")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_events_undispatched", "id", postgresql_where=sql_text("dispatched_at IS NULL")),
        Index(
            "ix_events_dispatched_at",
            "dispatched_at",
            postgresql_where=sql_text("dispatched_at IS NOT NULL"),
        ),
        {"comment": "The outbox: what happened in the store, for event triggers"},
    )


class SchemaChangeRequest(StoreBase):
    """An agent's request to change the store's `data` schema (`aios.datastores.schema_changes`):
    the spec it asked for, the SQL it compiles to, and what a member and the platform did with
    it. The run that asked waits on it (a LangGraph interrupt) until it's decided."""

    __tablename__ = "schema_change_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sql_text("gen_random_uuid()")
    )
    # The spec (`SchemaChange`, checked), and the SQL it compiles to: what's approved, and run.
    ops: Mapped[list] = mapped_column(JSONB)
    sql: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    # The agent's explanation, for the members.
    reason: Mapped[str] = mapped_column(Text)
    # The agent run that asked, and the tool call it asked in (so the call, run again when the
    # run resumes, finds its request rather than making another); cleared when the run goes.
    requested_by_run: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(255))
    # The store's data schema revision it was checked against (`schema_changes.read_revision`).
    base_revision: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[SchemaChangeStatus] = mapped_column(
        Enum(SchemaChangeStatus, name="schema_change_status"),
        default=SchemaChangeStatus.pending,
        server_default=SchemaChangeStatus.pending.value,
    )
    # The member who decided it.
    decided_by_user_id: Mapped[int | None] = mapped_column(Integer)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The note of the member who rejected it.
    note: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why applying it failed, or why it's stale.
    error: Mapped[str | None] = mapped_column(Text)
    # When the run that asked was given the outcome, after which it takes new messages again.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "requested_by_run", "tool_call_id", name="uq_schema_change_requests_run_tool_call"
        ),
        Index("ix_schema_change_requests_requested_by_run", "requested_by_run"),
        {"comment": "Agents' requests to change the data schema, and what was decided"},
    )


class SourceKind(enum.StrEnum):
    upload = "upload"
    url = "url"
    webhook = "webhook"
    email = "email"
    connector = "connector"


class SourceStatus(enum.StrEnum):
    active = "active"
    paused = "paused"
    failing = "failing"
    needs_reauth = "needs_reauth"
    removing = "removing"


# The one source every store has, which every uploaded document belongs to (`aios.sources`).
UPLOADS_NAME = "Uploads"


class Source(StoreBase):
    """Something that brings content into the store (`aios.sources`). Every store has exactly
    one upload source, Uploads, made with the store (the store tree's 0011): every uploaded
    document belongs to it, and it can't be paused or removed. Its settings never hold secrets."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind, name="source_kind"))
    name: Mapped[str] = mapped_column(String(255))
    settings: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status"), server_default=SourceStatus.active.value
    )
    # How often it syncs, in seconds; null for Uploads, which doesn't.
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    # Where its last committed sync got to, as its kind's adapter writes it.
    cursor: Mapped[str | None] = mapped_column(Text)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Syncs failed in a row.
    failure_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # The member who added it, by platform user id; null for Uploads.
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Exactly one Uploads: the migration makes it, and no second can be added.
        Index(
            "uq_sources_upload",
            "kind",
            unique=True,
            postgresql_where=sql_text("kind = 'upload'"),
        ),
        {"comment": "What brings content into the store: Uploads, and later synced sources"},
    )


class AuditEntry(StoreBase):
    """One entry of the audit log (`aios.audit`): who did what to what, and when. Append-only:
    the read-write role may only insert and read, and a trigger refuses updates, deletes and
    truncation even to the store's owner. Its details are on the deny list, so the data tools
    read only the rest (and agents read the reporting view, `reporting.audit_log`)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When it was done."
    )
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        Enum(AuditActorKind, name="audit_actor_kind"), comment="person, agent or system."
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, comment="The person who did it (aios.people.id)."
    )
    actor_agent_id: Mapped[int | None] = mapped_column(
        Integer, comment="The agent that did it (aios.agents.id)."
    )
    actor_run_id: Mapped[int | None] = mapped_column(
        Integer, comment="The agent run it was done in (aios.agent_runs.id)."
    )
    action: Mapped[str] = mapped_column(
        Text, comment="What was done, dotted: agent.archived, approval.decided, data.written."
    )
    target_kind: Mapped[str | None] = mapped_column(
        Text, comment="What kind of thing it was done to: agent, member, table, ..."
    )
    target_id: Mapped[str | None] = mapped_column(Text, comment="Which one: its id, or name.")
    details: Mapped[dict] = mapped_column(
        JSONB, server_default=sql_text("'{}'::jsonb"), default=dict
    )
    # The platform outbox row it came from (`aios.models.AuditOutbox`), so delivering one twice
    # records it once.
    outbox_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("outbox_id", name="uq_audit_log_outbox_id"),
        Index("ix_audit_log_at_id", "at", "id"),
        Index("ix_audit_log_action", "action"),
        Index("ix_audit_log_actor_user_id", "actor_user_id"),
        {"comment": "The audit log: who did what, and when. Append-only."},
    )


class ApprovalRule(StoreBase):
    """The least role that may decide a kind of schema change (`aios.approval_rules`), where an
    admin raised it above its default; a kind with no row has its default."""

    __tablename__ = "approval_rules"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    least_role: Mapped[str] = mapped_column(Text, comment="editor or admin.")
    updated_by: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("least_role IN ('editor', 'admin')", name="ck_approval_rules_least_role"),
        {"comment": "Who may decide each kind of schema change, where raised from the default"},
    )


class TableOwner(StoreBase):
    """A data table's owning agent, or none (`aios.datastores.ownership`): only the owner, and
    the agents it's shared with, may write an owned table. Named as the table is now in `data`:
    a dropped table's row goes by its dropped name until it's restored, and goes when it's
    purged."""

    __tablename__ = "table_owners"

    table_name: Mapped[str] = mapped_column(
        Text, primary_key=True, comment="The data table, as named in the data schema now."
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        comment="The agent that owns it (aios.agents.id); null: no owner.",
    )
    set_by: Mapped[int | None] = mapped_column(
        Integer, comment="The person who set it (aios.people.id); null: a schema change did."
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When it was set."
    )

    __table_args__ = (
        Index("ix_table_owners_agent_id", "agent_id"),
        {"comment": "Each data table's owning agent, or none"},
    )


class TableShare(StoreBase):
    """An agent a data table is shared with, which may write it as its owner may. Hangs off the
    table's ownership row, which carries it through renames (drop, restore) and deletion
    (purge)."""

    __tablename__ = "table_shares"

    table_name: Mapped[str] = mapped_column(
        ForeignKey("table_owners.table_name", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
        comment="The data table, as named in the data schema now.",
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The agent it's shared with (aios.agents.id).",
    )
    granted_by: Mapped[int | None] = mapped_column(
        Integer, comment="The person who shared it (aios.people.id)."
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When it was shared."
    )

    __table_args__ = (
        Index("ix_table_shares_agent_id", "agent_id"),
        {"comment": "The agents each data table is shared with, who may write it too"},
    )


VERDICT = Enum(Verdict, name="verdict")


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
    # The source it belongs to: Uploads for an upload. Required.
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Jev's verdict (`aios.verdicts`): awaiting_check until Jev checks it; only a clean or
    # released document is found by search and reaches context. Copied onto its search items.
    verdict: Mapped[Verdict] = mapped_column(
        VERDICT,
        server_default=Verdict.awaiting_check.value,
        default=Verdict.awaiting_check,
        comment="Jev's verdict: awaiting_check, clean, flagged or released.",
    )
    verdict_reason: Mapped[str | None] = mapped_column(Text, comment="Why Jev flagged it.")
    verdict_version: Mapped[str | None] = mapped_column(
        Text, comment="The version of Jev (model and policy) that judged it."
    )
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When Jev last judged it."
    )
    content_hash: Mapped[str | None] = mapped_column(
        Text, comment="The SHA-256 (hex) of the content Jev last judged."
    )
    released_by: Mapped[int | None] = mapped_column(
        Integer, comment="The admin who released it (aios.people.id)."
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When it was released."
    )
    released_content_hash: Mapped[str | None] = mapped_column(
        Text, comment="The SHA-256 (hex) of the content released."
    )

    source: Mapped[Source] = relationship()
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chunk.ordinal",
    )

    __table_args__ = (
        Index("ix_documents_source_id", "source_id"),
        Index("ix_documents_verdict", "verdict"),
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
    # No longer written: its search item (`SearchItem`) is embedded instead.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
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


class SearchItemKind(enum.StrEnum):
    document_chunk = "document_chunk"
    chat_message = "chat_message"
    run_message = "run_message"
    brief = "brief"


class SearchItem(StoreBase):
    """One item of the search index (`aios.search_index`): a document's chunk, a chat message, a
    user's or assistant's message of a run, or a handoff's brief. Written in the same transaction
    as its source row, and deleted with it (a foreign key per kind, of which exactly the kind's
    is set). Embedded in the background (`aios.search_index.embed_pending`): until then it's
    found by keyword. No visibility: everyone in the organisation can find every item."""

    __tablename__ = "search_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[SearchItemKind] = mapped_column(
        Enum(SearchItemKind, name="search_item_kind"),
        comment="document_chunk, chat_message, run_message or brief.",
    )
    # Its source row: the one matching its kind.
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), comment="A document chunk's chunk."
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), comment="A chat message's message."
    )
    turn_id: Mapped[int | None] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), comment="A run message's turn."
    )
    position: Mapped[int | None] = mapped_column(
        Integer, comment="A run message's place in its run's conversation, from 0."
    )
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invocations.id", ondelete="CASCADE"), comment="A brief's invocation."
    )
    # Extra reference data: a chunk's headings and pages, a run message's role.
    ref: Mapped[dict] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"), default=dict)
    # For filters and links: what it belongs to, where there's one.
    document_id: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[int | None] = mapped_column(Integer)
    agent_run_id: Mapped[int | None] = mapped_column(Integer)
    author_user_id: Mapped[int | None] = mapped_column(
        Integer, comment="The person who wrote it, if a person did."
    )
    text: Mapped[str] = mapped_column(Text, comment="Its text, as shown.")
    search_text: Mapped[str | None] = mapped_column(
        Text, comment="What's searched and embedded instead of its text, if not its text."
    )
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(search_text, text))", persisted=True),
    )
    text_hash: Mapped[str] = mapped_column(
        Text, comment="The SHA-256 (hex) of what's searched: unchanged text isn't re-embedded."
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), comment="What's searched's embedding; null until embedded."
    )
    verdict: Mapped[Verdict] = mapped_column(
        VERDICT,
        server_default=Verdict.awaiting_check.value,
        comment="Its source's Jev verdict: found by search only if clean, released or derived.",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(kind = 'document_chunk') = (chunk_id IS NOT NULL)"
            " AND (kind = 'chat_message') = (message_id IS NOT NULL)"
            " AND (kind = 'run_message') = (turn_id IS NOT NULL AND position IS NOT NULL)"
            " AND (kind = 'brief') = (invocation_id IS NOT NULL)",
            name="ck_search_items_source",
        ),
        UniqueConstraint("chunk_id", name="uq_search_items_chunk_id"),
        UniqueConstraint("message_id", name="uq_search_items_message_id"),
        UniqueConstraint("turn_id", "position", name="uq_search_items_turn_id_position"),
        UniqueConstraint("invocation_id", name="uq_search_items_invocation_id"),
        Index(
            "ix_search_items_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_search_items_tsv", "tsv", postgresql_using="gin"),
        # What the embedding sweep picks up, and where it looks for an embedding to reuse.
        Index("ix_search_items_unembedded", "id", postgresql_where=sql_text("embedding IS NULL")),
        Index(
            "ix_search_items_text_hash",
            "text_hash",
            postgresql_where=sql_text("embedding IS NOT NULL"),
        ),
        Index("ix_search_items_created_at", "created_at"),
        Index("ix_search_items_chat_id", "chat_id"),
        Index("ix_search_items_agent_id", "agent_id"),
        Index("ix_search_items_agent_run_id", "agent_run_id"),
        # Where `query_data` finds the stored verdict of text it read (`aios.tools.data`).
        Index("ix_search_items_text_md5", func.md5(sql_text("text"))),
        Index(
            "ix_search_items_search_text_md5",
            func.md5(sql_text("search_text")),
            postgresql_where=sql_text("search_text IS NOT NULL"),
        ),
        {"comment": "The search index: documents, chat messages, runs' messages and briefs"},
    )


class UsageEntry(StoreBase):
    """One billable unit (`aios.usage`): a model call, an embedding batch, a provider's web
    searches in one response, a tool call or a sandbox session's seconds, with what it cost in
    pounds sterling and who and what it's charged to. Never pruned. Agents read it through
    `reporting.usage`."""

    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When it was recorded."
    )
    kind: Mapped[UsageKind] = mapped_column(
        Enum(UsageKind, name="usage_kind"),
        comment="model_call, embedding, web_search, tool_call or sandbox_seconds.",
    )
    purpose: Mapped[UsagePurpose] = mapped_column(
        Enum(UsagePurpose, name="usage_purpose"),
        comment="turn, handoff, handback, compaction, ingest, search_query, index_embedding or"
        " insight.",
    )
    model: Mapped[str | None] = mapped_column(Text, comment="The model, as provider:model.")
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=sql_text("0"), default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=sql_text("0"), default=0)
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, server_default=sql_text("0"), default=0
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, server_default=sql_text("0"), default=0
    )
    quantity: Mapped[float] = mapped_column(
        Double,
        server_default=sql_text("1"),
        default=1,
        comment="How many: calls, texts embedded, searches, or seconds.",
    )
    cost: Mapped[float | None] = mapped_column(
        Double, comment="Estimated cost in GBP; null for a model with no price."
    )
    agent_id: Mapped[int | None] = mapped_column(Integer, comment="aios.agents.id")
    workflow_id: Mapped[int | None] = mapped_column(Integer, comment="aios.workflows.id")
    agent_run_id: Mapped[int | None] = mapped_column(Integer, comment="aios.agent_runs.id")
    workflow_run_id: Mapped[int | None] = mapped_column(Integer, comment="aios.workflow_runs.id")
    turn_id: Mapped[int | None] = mapped_column(Integer, comment="aios.turns.id")
    invocation_id: Mapped[int | None] = mapped_column(Integer, comment="aios.invocations.id")
    person_id: Mapped[int | None] = mapped_column(
        Integer, comment="The person charged (aios.people.id)."
    )
    cause: Mapped[InvocationCause | None] = mapped_column(
        Enum(InvocationCause, name="invocation_cause"), comment="mention, schedule or manual."
    )
    # What makes recording it again a no-op (a model response's id): a retry never counts twice.
    call_key: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("call_key", name="uq_usage_call_key"),
        Index("ix_usage_at", "at"),
        Index("ix_usage_turn_id", "turn_id"),
        Index("ix_usage_workflow_run_id", "workflow_run_id"),
        {"comment": "The usage ledger: one row per billable unit, priced in GBP. Never pruned."},
    )
