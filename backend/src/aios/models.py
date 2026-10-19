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
    # Moved to their organisation's store (`DataStore`, revision 0035): no longer read, and
    # dropped by the contract step.
    export_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    embed_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Organisation(Base):
    """A group of users, which owns what its members make (`aios.organisations`).

    Every user is in exactly one organisation at a time: a personal one, made when they sign up
    (or leave another), or one they joined through an invitation link. Nothing in an organisation
    is private to one member. The creator is the one member who may rename it and, later, invite
    others: a stopgap until roles exist.
    """

    __tablename__ = "organisations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique among all organisations: a personal one is named after its user's login, suffixed
    # when that's taken.
    name: Mapped[str] = mapped_column(String(255), unique=True)
    # Made for one user, on sign-up or on leaving another organisation. An ordinary organisation
    # all the same (it can be renamed and, later, invited into); the flag only decides whether it
    # may be deleted when its one member joins another.
    personal: Mapped[bool] = mapped_column(server_default=text("false"))
    # Null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])
    members: Mapped[list["OrganisationMember"]] = relationship(
        back_populates="organisation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrganisationMember.joined_at",
    )


class OrganisationInvite(Base):
    """A shareable invitation link into an organisation. The link carries a random token; only
    its hash is stored, so the link is shown once, when it's made."""

    __tablename__ = "organisation_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set when the creator cuts the link off; a revoked link opens for no one.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganisationMember(Base):
    """A user's membership of an organisation. A user has one at a time (the unique index on
    the user), though the table allows several, for later."""

    __tablename__ = "organisation_members"

    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # The invitation link they came in through, if any (null for a creator, or once the link's
    # row is gone).
    invite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisation_invites.id", ondelete="SET NULL")
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organisation: Mapped[Organisation] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()

    __table_args__ = (
        # One organisation per user, for now (`aios.auth.current_organisation` reads the one):
        # drop this to allow several.
        Index("uq_organisation_members_user_id", "user_id", unique=True),
    )


class DataStoreStatus(enum.StrEnum):
    # Its database is being created (`aios.datastores.provision`); nothing can use it yet.
    provisioning = "provisioning"
    ready = "ready"
    # Provisioning gave up; re-running it (`aios.datastores.provision.retry_unfinished` does, every
    # few minutes, as does the deploy's `migrate_all`) carries on.
    failed = "failed"
    # Its organisation was deleted (an empty one, on accepting an invitation): the provisioning
    # worker's sweep drops its database, roles and files (`provision.delete_deleting`) ...
    deleting = "deleting"
    # ... and then it's this: the row is kept, as a record of the store.
    deleted = "deleted"


class DataStore(Base):
    """Where an organisation's data lives: a database of its own (see `aios.datastores`).

    It has an id of its own, rather than being its owner's, so a store could change hands (it
    did, from users to organisations) without renaming anything named after it: its database
    and roles are named after this id, never its owner's. Its documents and chunks are in its
    database (`aios.datastores.models`), not here.
    """

    __tablename__ = "data_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # The organisation whose store it is: one each. Null only once it's `deleting` (its
    # organisation was deleted) or `deleted`.
    owner_organisation_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="SET NULL"), unique=True
    )
    # Whose it was before stores were organisations' (revision 0035): no longer written, and
    # dropped by the contract step.
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), unique=True
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
    # Copy the organisation's agent runs and workflow runs into the store's `history` schema
    # (`aios.datastores.history`, on the Organisation page).
    export_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    # Embed the exported messages too, so agents can search past runs by meaning. Only while
    # the export is on: switching it off switches this off.
    embed_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    # Why provisioning, or the last migration, failed.
    error: Mapped[str | None] = mapped_column(Text)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # What it uses, as last measured (`aios.datastores.quota`, on a schedule): the data its
    # tables hold and its documents' files' bytes. NULL until first measured.
    db_bytes: Mapped[int | None] = mapped_column(BigInteger)
    files_bytes: Mapped[int | None] = mapped_column(BigInteger)
    usage_measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "owner_organisation_id IS NOT NULL OR status IN ('deleting', 'deleted')",
            name="ck_data_stores_owner_organisation",
        ),
    )


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
    # The organisation it belongs to (`aios.organisations`), and what it answers to. Nullable
    # until the contract step makes it NOT NULL and drops `owner_id`, which saves still write
    # (as the creator) so the release before keeps working meanwhile.
    organisation_id: Mapped[int | None] = mapped_column(
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
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The organisation it belongs to (`aios.organisations`), and what it answers to. Nullable
    # until the contract step makes it NOT NULL and drops `owner_id`, which saves still write
    # (as the creator) so the release before keeps working meanwhile.
    organisation_id: Mapped[int | None] = mapped_column(
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
    # Which of the app's built-in agents it is, if it's one: made for its owner by the app rather
    # than by them (`aios.assistant`), and found again by this. Null for the owner's own agents.
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
    # The organisation it belongs to (`aios.organisations`), and what it answers to. Nullable
    # until the contract step makes it NOT NULL and drops `owner_id`, which saves still write
    # (as the creator) so the release before keeps working meanwhile.
    organisation_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
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
    # Which member deleted it; null once that user is deleted.
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    current_version: Mapped["WorkflowVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    __table_args__ = (
        # Names are unique among the organisation's live workflows, so a deleted workflow's name
        # can be reused.
        Index(
            "uq_workflows_organisation_id_name_live",
            "organisation_id",
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
    # The member who started it; null for a schedule's run, or once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
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
    # The organisation it belongs to (`aios.organisations`), and what it answers to. Nullable
    # until the contract step makes it NOT NULL and drops `owner_id`, which saves still write
    # (as the creator) so the release before keeps working meanwhile.
    organisation_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
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


# Chats are group conversations between users, agents and workflows. A member row says who is
# in a chat; which kind it is decides which id column is set, like a trigger's target, so agents
# and workflows can be members as well as users. Only users have a role: the owner (one per chat,
# the user who made it) can change the chat, remove members and delete it.


class ChatVisibility(enum.StrEnum):
    public = "public"
    private = "private"


class ChatMemberKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"


class ChatMemberRole(enum.StrEnum):
    owner = "owner"
    member = "member"


class Chat(Base):
    """A group conversation. Its members are `ChatMember` rows, its messages `ChatMessage`s."""

    __tablename__ = "chats"
    __table_args__ = (
        # Unique within the organisation: a chat is named like a channel, not like one member's
        # own thing, and two organisations can each have "general".
        Index("uq_chats_organisation_id_name", "organisation_id", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The organisation it belongs to (`aios.organisations`). Nullable while `owner_id` is still
    # what it answers to: the upgrade stamped the rows from before, saves stamp new ones once
    # they're scoped, and the contract step makes it NOT NULL and drops `owner_id`.
    organisation_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    # A private chat doesn't exist for a user who isn't a member of it.
    visibility: Mapped[ChatVisibility] = mapped_column(Enum(ChatVisibility, name="chat_visibility"))
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


class ChatMember(Base):
    """Who is in a chat: a user, an agent or a workflow (all of the chat's organisation)."""

    __tablename__ = "chat_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    kind: Mapped[ChatMemberKind] = mapped_column(Enum(ChatMemberKind, name="chat_member_kind"))
    # Exactly one is set, matching `kind`. Agents and workflows are soft-deleted; the cascades are
    # for deleting a user, which deletes their agents and workflows too.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    # Users only: the chat's owner, or an ordinary member.
    role: Mapped[ChatMemberRole | None] = mapped_column(
        Enum(ChatMemberRole, name="chat_member_role")
    )
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Users only: the id of the last of the chat's messages they've seen (null: none yet), from
    # which their unread count is counted. A cursor, not a reference: it only goes up.
    last_read_message_id: Mapped[int | None]

    chat: Mapped[Chat] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    agent: Mapped[Agent | None] = relationship()
    workflow: Mapped[Workflow | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL AND workflow_id IS NULL)"
            " OR (kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL"
            " AND workflow_id IS NULL)"
            " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND user_id IS NULL"
            " AND agent_id IS NULL)",
            name="ck_chat_members_target",
        ),
        CheckConstraint(
            "(kind = 'user') = (role IS NOT NULL)",
            name="ck_chat_members_role",
        ),
        UniqueConstraint("chat_id", "user_id"),
        UniqueConstraint("chat_id", "agent_id"),
        UniqueConstraint("chat_id", "workflow_id"),
    )


class ChatAuthorKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"
    # The chat itself (no author), for messages about it.
    system = "system"


class ChatMessageKind(enum.StrEnum):
    # Markdown, as written.
    text = "text"
    # An acknowledgement from the agent or workflow a mention started, with a card under it
    # following the run (`invocation_id`).
    run_card = "run_card"


class ChatMessage(Base):
    """A message in a chat, read in id order: `?after=` and `?before=` page by id."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    author_kind: Mapped[ChatAuthorKind] = mapped_column(
        Enum(ChatAuthorKind, name="chat_author_kind")
    )
    # At most one is set, matching `author_kind`; none for a system message. Deleting the author
    # leaves what they posted, with no author.
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    author_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    author_workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[ChatMessageKind] = mapped_column(Enum(ChatMessageKind, name="chat_message_kind"))
    body: Mapped[str] = mapped_column(Text)
    # Who the message mentions, `[{kind, id, name}]`, with the names as they were when posted.
    mentions: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The invocation it's part of: a run card's, or the result the run posted back. Deleted with
    # the chat, as the invocation is.
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "chat_invocations.id",
            ondelete="CASCADE",
            use_alter=True,
            name="fk_chat_messages_invocation_id",
        ),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    author_user: Mapped[User | None] = relationship()
    author_agent: Mapped[Agent | None] = relationship()
    author_workflow: Mapped[Workflow | None] = relationship()

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
    )


class ChatInvocationStatus(enum.StrEnum):
    # Waiting for its run to start (`aios.invocations`).
    queued = "queued"
    # The handoff is briefing its run on the chat (`aios.handoff`); the run starts next.
    briefing = "briefing"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"

    @property
    def finished(self) -> bool:
        return self in (ChatInvocationStatus.succeeded, ChatInvocationStatus.failed)


class ChatInvocation(Base):
    """A run of an agent or workflow that a mention in a chat started (`aios.invocations`): one
    per mention, shaped like a trigger's outcome but one event rather than a standing rule. Its
    card follows the run; when the run ends, its result is posted back and it finishes."""

    __tablename__ = "chat_invocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    # The message that mentioned it.
    message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    # Who mentioned it. The run executes as its agent's or workflow's owner, as runs do.
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="chat_invocation_target_kind")
    )
    # Exactly one is set, matching `target_kind`. Agents and workflows are soft-deleted; the
    # cascades are for deleting a user, which deletes their agents and workflows too.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ChatInvocationStatus] = mapped_column(
        Enum(ChatInvocationStatus, name="chat_invocation_status"),
        default=ChatInvocationStatus.queued,
    )
    # Why it failed: the run's error, or why it couldn't start.
    error: Mapped[str | None] = mapped_column(Text)
    # What the handoff told its run of the chat (`aios.handoff`); null until it has.
    brief: Mapped[str | None] = mapped_column(Text)
    # The run it started; null until it has, and once that run has been deleted (the card says
    # so). Deleting the chat leaves the run.
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), unique=True
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When its run started (null if it never did), and when it finished.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # An agent's: set when a process claims it to run its turn (`aios.firing`) and refreshed
    # while the turn runs; one whose heartbeat has stopped lost its process, and is failed. Null
    # while it waits for a free slot, and for a workflow's, whose run the worker looks after.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[Workflow | None] = relationship()
    agent: Mapped[Agent | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_chat_invocations_target",
        ),
        # A chat's unfinished invocations: what its poll reports on.
        Index(
            "ix_chat_invocations_unfinished",
            "chat_id",
            postgresql_where=text("status IN ('queued', 'briefing', 'running')"),
        ),
    )
