"""The platform's audit outbox: audit entries for organisations' stores, written by platform-side
actions in their own transaction and delivered by `aios.audit.drain`."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Identity, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class AuditActorKind(enum.StrEnum):
    """Who took an audited action: a person, an agent (in a run), or the platform itself."""

    person = "person"
    agent = "agent"
    system = "system"


class AuditOutbox(Base):
    """An audit entry (`aios.datastores.models.AuditEntry`) waiting to be delivered to its
    organisation's store (`aios.audit`): deleted once it's there. It goes with its
    organisation, whose store goes too."""

    __tablename__ = "audit_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        Enum(AuditActorKind, name="audit_actor_kind")
    )
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_agent_id: Mapped[int | None] = mapped_column(Integer)
    actor_run_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    target_kind: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), default=dict)
