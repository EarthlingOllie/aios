"""The run service's work queue (`aios.run_service`): one row per item of work waiting to run or
running, in the platform database, so every run service process claims from one queue fairly
across organisations. An item is deleted once it's done; its outcome lives in the store."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class WorkLane(enum.StrEnum):
    """Who waits on an item: people in a chat (`interactive`, mentions) or nobody
    (`background`: schedules, events, workflow advances and nodes, resumes, embedding sweeps;
    `sources`: phase 4's syncs, so a big sync waits on its own share). Claiming serves
    interactive work first, and fairly between organisations within each lane; background and
    sources work never take interactive work's reserved capacity."""

    interactive = "interactive"
    background = "background"
    sources = "sources"


class WorkKind(enum.StrEnum):
    """What an item does (`aios.run_service.work`)."""

    # A mention's invocation: the handoff, then the agent's first turn (enqueued as its own
    # item) or the workflow's run.
    invocation_start = "invocation_start"
    # One turn of an agent run: a mentioned agent's first turn, or a schedule's.
    agent_turn = "agent_turn"
    # A workflow run's coordinator step: rebuilds its state from its recorded steps, records
    # what's finished, and starts the nodes now ready (`aios.workflows.advance`).
    workflow_advance = "workflow_advance"
    # One agent node of a workflow run: its turn, as a child invocation of the run's.
    workflow_node = "workflow_node"
    # A standalone agent run waiting on schema change requests, carried on once one is decided.
    resume_turn = "resume_turn"
    # Embedding a store's search items still waiting for an embedding, under a time budget
    # (`aios.search_index.embed_pending`): background work, one queued per store at a time.
    embedding_sweep = "embedding_sweep"
    # One sync of a source (`aios.sync`): sources work, one queued per source at a time.
    source_sync = "source_sync"


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
    # Enqueued again while it ran (`Work.again_if_running`): it runs once more when it's done.
    again: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)

    __table_args__ = (
        # What claiming scans: the ready items.
        Index("ix_work_items_ready_at", "ready_at"),
    )
