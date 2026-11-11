"""Identity and organisations: users, organisations, their members and invitations."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aios.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Trimmed and non-empty (`aios.auth.Name`). The app shows them by both (`full_name`).
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    # What they sign in with: stored normalised (`aios.auth.Email`), so it's unique in any case.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # An Argon2id PHC string (`aios.passwords`).
    password_hash: Mapped[str] = mapped_column(Text)
    # Copied into each session at sign-in (`aios.auth.start_session`): a session holding another
    # value is signed out, so bumping it signs out every session the user has.
    session_version: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def full_name(self) -> str:
        """How the app shows the user, wherever it names them: "First Last"."""
        return f"{self.first_name} {self.last_name}"


class Organisation(Base):
    """A group of users, which owns what its members make (`aios.organisations`).

    Every user is in exactly one organisation at a time: a personal one, made when they sign up
    (or leave another), or one they joined through an invitation link. Nothing in an organisation
    is private to one member. The creator is the one member who may rename it and, later, invite
    others: a stopgap until roles exist.
    """

    __tablename__ = "organisations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique among all organisations: a personal one is named after its user's full name, suffixed
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
