"""Triggers: what starts agents and workflows without a user in the loop."""

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base

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
    # The organisation it belongs to (`aios.organisations`), and what it answers to: every
    # member's, and gone with the organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[TriggerKind] = mapped_column(Enum(TriggerKind, name="trigger_kind"))
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="trigger_target_kind")
    )
    # Exactly one is set, matching `target_kind`: the agent or workflow, in the organisation's
    # store (`aios.datastores.models`), so not a foreign key. They're archived, never deleted.
    agent_id: Mapped[int | None] = mapped_column(Integer, index=True)
    workflow_id: Mapped[int | None] = mapped_column(Integer, index=True)
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
    # The agent run it last started, in its organisation's store, by id (not a foreign key);
    # cleared when that run is deleted.
    last_run_id: Mapped[int | None] = mapped_column(Integer)
    # The workflow run it last started, the same way; its status is the schedule's
    # (`api.triggers.trigger_out`), since a worker runs it after the schedule has fired.
    last_workflow_run_id: Mapped[int | None] = mapped_column(Integer)
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
        # What the scheduler polls for.
        Index("ix_triggers_due", "next_run_at", postgresql_where=text("next_run_at IS NOT NULL")),
    )
