"""The run service's work queue (`aios.run_service`): one row per item of work waiting to run or
running, in the platform database, so every run service process claims from one queue fairly
across organisations. An item is deleted once it's done; its outcome lives in the store."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Identity, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class WorkLane(enum.StrEnum):
    """Who waits on an item: people in a chat (`interactive`, mentions) or nobody
    (`background`: schedules, and later workflow nodes, resumes and embedding sweeps). Claiming
    serves interactive work first."""

    interactive = "interactive"
    background = "background"


class WorkKind(enum.StrEnum):
    """What an item does (`aios.run_service.work`)."""

    # A mention's invocation: the handoff, then the agent's first turn (enqueued as its own
    # item) or the workflow's run.
    invocation_start = "invocation_start"
    # One turn of an agent run: a mentioned agent's first turn, or a schedule's.
    agent_turn = "agent_turn"


class WorkItem(Base):
    """An item of work for the run service, for an organisation's store, named by store ids in
    its `payload`. Ready from `ready_at`; a run service process that claims it leases it
    (`lease_token`, `leased_until`), and each claim counts an attempt. An item whose lease has
    lapsed (its process died) is claimable again, until its attempts run out."""

    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_stores.id", ondelete="CASCADE"), index=True
    )
    lane: Mapped[WorkLane] = mapped_column(Enum(WorkLane, name="work_lane"))
    kind: Mapped[WorkKind] = mapped_column(Enum(WorkKind, name="work_kind"))
    payload: Mapped[dict] = mapped_column(JSONB)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Enqueueing the same work twice adds it once (`aios.run_service.enqueue`).
    dedup_key: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        # What claiming scans: the ready items.
        Index("ix_work_items_ready_at", "ready_at"),
    )
