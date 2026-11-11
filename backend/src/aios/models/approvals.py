"""Approvals: agents' schema change requests, which a member decides."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aios.models.base import Base
from aios.models.stores import DataStore


class SchemaChangeStatus(enum.StrEnum):
    pending = "pending"  # waiting for a member to decide it
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
    the spec it asked for, the SQL it compiles to, and what a member and the platform did
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
    # The agent's explanation, for the members.
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
    # The note of the member who rejected it.
    note: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why applying it failed, or why it's stale.
    error: Mapped[str | None] = mapped_column(Text)
    # When the run that asked was given the outcome, after which it takes new messages again.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    data_store: Mapped[DataStore] = relationship()

    __table_args__ = (UniqueConstraint("requested_by_run", "tool_call_id"),)
