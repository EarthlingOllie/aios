"""Triggers: what starts agents and workflows without a user in the loop.

Triggers themselves are in each organisation's store (`aios.datastores.models.Trigger`). The
platform keeps only the schedule due index (`ScheduleDue`): when each enabled schedule is next
due, so the scheduler can find what's due across every store with one indexed query
(`aios.schedules`).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class TriggerKind(enum.StrEnum):
    schedule = "schedule"


class TriggerTargetKind(enum.StrEnum):
    agent = "agent"
    workflow = "workflow"


class TriggerStatus(enum.StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ScheduleDue(Base):
    """When an enabled schedule in an organisation's store is next due: a derived copy of the
    trigger's `next_run_at`, written after the store commits whenever the schedule is saved or
    claimed, and rebuilt from the store's triggers by the repair sweep (`aios.schedules`). A
    disabled schedule has no row. The store stays the source of truth: firing re-reads the
    trigger there, under its row lock."""

    __tablename__ = "schedule_due"

    data_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_stores.id", ondelete="CASCADE"), primary_key=True
    )
    # The trigger's id in that store (ids are each store's own).
    trigger_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # What the scheduler polls for.
        Index("ix_schedule_due_next_run_at", "next_run_at"),
    )
