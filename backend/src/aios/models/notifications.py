"""Notifications: things in the app for a user to see."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class Notification(Base):
    """Something in the app for a user to see (`aios.notifications`): what kind it is, and a
    reference to what it's about in its organisation (a chat message, a run, an approval), never
    its content, which is read from the organisation's store when it's listed. It's read once
    the user marks it so, or once what it's about no longer needs them (an approval decided, a
    chat read past the message). At most one is unread per person, organisation, kind and
    reference."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The organisation it's from: a person's notifications from every organisation they're in
    # are listed together, each opening in its own. It goes with its organisation.
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    ref: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # A user's, unread first: what the SPA polls for.
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        # The notifications about something (`ref @> ...`), to mark them read.
        Index("ix_notifications_ref", "ref", postgresql_using="gin"),
        # At most one unread about the same thing: a repeat adds nothing.
        Index(
            "ux_notifications_unread",
            "user_id",
            "organisation_id",
            "kind",
            "ref",
            unique=True,
            postgresql_where=text("read_at IS NULL"),
        ),
    )
