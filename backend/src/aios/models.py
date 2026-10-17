import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    # Copy their agent runs into their data store's `history` schema (`aios.datastores.history`).
    export_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    # Embed the exported messages too, so agents can search past runs by meaning. Only while
    # the export is on: switching it off switches this off.
    embed_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataStoreStatus(enum.StrEnum):
    # Its database is being created (`aios.datastores.provision`); nothing can use it yet.
    provisioning = "provisioning"
    ready = "ready"
    # Provisioning gave up; re-running it (`aios.datastores.provision.retry_unfinished` does, every
    # few minutes, as does the deploy's `migrate_all`) carries on.
    failed = "failed"
    # For deleting users' stores (not yet used).
    deleting = "deleting"
    deleted = "deleted"


class DataStore(Base):
    """Where a user's data lives: a database of its own (see `aios.datastores`).

    It has an id of its own, rather than being the user's, so a store can later change hands
    (to a team) without renaming anything named after it: its database and roles are named
    after this id, never a user's. Its documents and chunks are in its database
    (`aios.datastores.models`), not here.
    """

    __tablename__ = "data_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # One store per user, for now. Teams will own stores too (an `owner_team_id`).
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[DataStoreStatus] = mapped_column(
        Enum(DataStoreStatus, name="data_store_status"),
        default=DataStoreStatus.provisioning,
        server_default=DataStoreStatus.provisioning.value,
    )
    # Where its database is, if not on the platform database's instance (where every store is,
    # for now, and NULL says so): kept so a store can later move to another instance.
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    # `ds_<id as hex>`, and its roles `<role_prefix>_owner`, `_rw` and `_ro`.
    dbname: Mapped[str] = mapped_column(String(63), unique=True)
    role_prefix: Mapped[str] = mapped_column(String(56), unique=True)
    # The login roles' passwords, encrypted (`aios.datastores.credentials`). Never logged, and
    # never returned by the API.
    rw_password: Mapped[str | None] = mapped_column(Text)
    ro_password: Mapped[str | None] = mapped_column(Text)
    # The store tree's Alembic revision its database is at (`aios.datastores.migrations`).
    schema_revision: Mapped[str | None] = mapped_column(String(64))
    # The revision of the tree's `history` branch it's at: null until its owner first switches
    # the run history export on, which creates the `history` schema.
    history_revision: Mapped[str | None] = mapped_column(String(64))
    # When the run history export last caught up (copied in whatever runs were pending).
    history_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When embedding the exported messages last caught up (embedded every one waiting); null
    # while it hasn't since it was switched on, or since it last failed.
    history_embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why embedding last failed, and when it's tried again (unless the store's export runs
    # first, after a turn); both cleared once it catches up.
    history_embed_error: Mapped[str | None] = mapped_column(Text)
    history_embed_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why provisioning, or the last migration, failed.
    error: Mapped[str | None] = mapped_column(Text)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # What it uses, as last measured (`aios.datastores.quota`, on a schedule): the data its
    # tables hold and its documents' files' bytes. NULL until first measured.
    db_bytes: Mapped[int | None] = mapped_column(BigInteger)
    files_bytes: Mapped[int | None] = mapped_column(BigInteger)
    usage_measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HistoryExportKind(enum.StrEnum):
    agent_run = "agent_run"
    workflow_run = "workflow_run"


class HistoryExport(Base):
    """The run history export's outbox: a run (by kind and platform id) to copy into the data
    store, or to delete from it if it's gone (`aios.datastores.history`).

    One row per run and store. It's written in the same transaction as the run's own update,
    and written again (`exported_at`, `error` and `retry_at` cleared) each time the run
    changes, so it's pending while `exported_at` is null. A run that failed to export keeps its
    `error`, and is set aside until `retry_at` (or until it changes again, if that's sooner), so
    it doesn't hold up the runs after it.
    """

    __tablename__ = "history_exports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_stores.id", ondelete="CASCADE")
    )
    kind: Mapped[HistoryExportKind] = mapped_column(
        Enum(HistoryExportKind, name="history_export_kind")
    )
    # Not a foreign key: the row outlives a deleted run, to delete its copy.
    source_id: Mapped[int] = mapped_column(BigInteger)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("data_store_id", "kind", "source_id"),
        # What the export drains.
        Index(
            "ix_history_exports_pending",
            "data_store_id",
            "enqueued_at",
            postgresql_where=text("exported_at IS NULL"),
        ),
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
    # Paused on a node waiting for a schema change request, holding no worker; queued again
    # once it's decided (`aios.workflows`).
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"


class WorkflowStepStatus(enum.StrEnum):
    running = "running"
    # An agent node waiting for a schema change request, or a workflow node one of whose nodes
    # is waiting.
    waiting = "waiting"
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
    # While it's waiting, since when; and how long it has waited in all before that. Waiting
    # doesn't count towards the run's time limit.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waited_seconds: Mapped[float] = mapped_column(Double, server_default=text("0"))

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
    # While an agent node waits: the schema change request it waits on. (By id, not through
    # its agent run, which goes if its agent is deleted meanwhile.)
    waiting_on_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schema_change_requests.id", ondelete="SET NULL"), index=True
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


class SchemaChangeStatus(enum.StrEnum):
    pending = "pending"  # waiting for the owner
    approved = "approved"  # being applied
    rejected = "rejected"
    expired = "expired"  # undecided for too long; counts as rejected
    applied = "applied"
    failed = "failed"  # applying it failed; the schema is unchanged
    stale = "stale"  # the schema changed since, and it no longer applies
    cancelled = "cancelled"  # the run that asked was deleted, or can't resume

    @property
    def decided(self) -> bool:
        """Whether this is an outcome the run that asked can be resumed with."""
        return self not in (SchemaChangeStatus.pending, SchemaChangeStatus.approved)


class SchemaChangeRequest(Base):
    """An agent's request to change its store's `data` schema (`aios.datastores.schema_changes`):
    the spec it asked for, the SQL it compiles to, and what the owner and the platform did
    with it. The run that asked waits on it (a LangGraph interrupt) until it's decided."""

    __tablename__ = "schema_change_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_stores.id", ondelete="CASCADE"), index=True
    )
    # The spec (`SchemaChange`, checked), and the SQL it compiles to: what's approved, and run.
    ops: Mapped[list] = mapped_column(JSONB)
    sql: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    # The agent's explanation, for the owner.
    reason: Mapped[str] = mapped_column(Text)
    # The agent run that asked, and the tool call it asked in (so the call, run again when the
    # run resumes, finds its request rather than making another).
    requested_by_run: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(255))
    # The store's data schema revision it was checked against (`schema_changes.read_revision`).
    base_revision: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[SchemaChangeStatus] = mapped_column(
        Enum(SchemaChangeStatus, name="schema_change_status"),
        default=SchemaChangeStatus.pending,
        server_default=SchemaChangeStatus.pending.value,
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The owner's note on a rejection.
    note: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why applying it failed, or why it's stale.
    error: Mapped[str | None] = mapped_column(Text)
    # When the run that asked was given the outcome, after which it takes new messages again.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    data_store: Mapped[DataStore] = relationship()

    __table_args__ = (UniqueConstraint("requested_by_run", "tool_call_id"),)


class Notification(Base):
    """Something in the app for a user to see (`aios.notifications`): what kind it is, and a
    reference to what it's about (for a schema change request, its id and its store's). Not tied
    to any one feature, so other kinds can use it later. It's read once the user marks it so, or
    once what it's about no longer needs them (a request decided)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(64))
    ref: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # A user's, unread first: what the SPA polls for.
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        # The notifications about something (`ref @> ...`), to mark them read.
        Index("ix_notifications_ref", "ref", postgresql_using="gin"),
    )
