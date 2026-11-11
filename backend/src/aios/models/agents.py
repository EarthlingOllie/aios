"""Agent runs and their sandbox sessions."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base

# Agents and skills themselves, with their versions, are in each organisation's store
# (`aios.datastores.models`). Runs stay here for now, naming the agent and version they ran by
# id within their organisation's store.


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
    # The organisation whose agent it ran (`aios.organisations`): its store holds the agent and
    # the version, and it goes with the organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # The agent, in the organisation's store (`aios.datastores.models.Agent`): ids repeat
    # across organisations, so it's named with `organisation_id`, and isn't a foreign key.
    agent_id: Mapped[int] = mapped_column(Integer, index=True)
    # Pinned when the run starts: editing the agent mid-conversation doesn't change the run.
    agent_version_id: Mapped[int] = mapped_column(Integer, index=True)
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
    # The turn lease (`aios.turn_lease`): the token of the turn that holds the run, and when its
    # hold lapses unless refreshed. Both null while no turn holds it; an expired one is free.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

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
