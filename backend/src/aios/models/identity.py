"""Identity and organisations: users, organisations, their members and invitations."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aios.models.base import Base


class Role(enum.StrEnum):
    """What a member may do in an organisation (`aios.permissions` has the table of what each
    may do). An organisation always has at least one admin."""

    admin = "admin"
    editor = "editor"
    participant = "participant"


# The column type of a role, on memberships and invitation links.
ROLE_TYPE = Enum(Role, name="organisation_role")


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

    A person is in any number of organisations: ones they created, and ones they joined through
    an invitation link. Nothing in an organisation is private to one member. The creator is the
    admin it starts with; what each member may do is their role (`OrganisationMember.role`).
    """

    __tablename__ = "organisations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Two organisations may share a name: the slug tells them apart.
    name: Mapped[str] = mapped_column(String(255))
    # What a link in the browser names it by (`/o/<slug>/...`; the API uses the id): made from
    # the name when it's created, suffixed on a clash (`aios.organisations.free_slug`), and
    # never changed by a rename, so a shared link keeps working.
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    # Null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Its deletion, while one is scheduled (`aios.organisations.schedule_deletion`): when and by
    # whom it was scheduled, and when the grace period ends and the sweep deletes it. All null
    # otherwise (never scheduled, or cancelled).
    deletion_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_scheduled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deletes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many of its items the run service runs at once (`aios.run_service`), set by an admin;
    # null for the default (`run_service_org_in_flight_cap`). Soft: concurrent claimers can
    # overshoot it by the few items each claims.
    in_flight_cap: Mapped[int | None] = mapped_column(Integer)
    # Retention (`aios.retention`): how many days its runs, and its chats' messages, are kept
    # once they've finished. Null keeps them indefinitely, the default. Only admins set them.
    run_retention_days: Mapped[int | None] = mapped_column(Integer)
    chat_retention_days: Mapped[int | None] = mapped_column(Integer)
    # Its caps on turns and workflow runs (`aios.limits`), where an admin overrode the
    # deployment's, by name; empty for all the defaults.
    limits: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))

    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])
    members: Mapped[list["OrganisationMember"]] = relationship(
        back_populates="organisation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrganisationMember.joined_at",
    )

    __table_args__ = (
        CheckConstraint(
            "(deletion_scheduled_at IS NULL) = (deletes_at IS NULL)",
            name="ck_organisations_deletion_scheduled",
        ),
        CheckConstraint(
            "run_retention_days BETWEEN 1 AND 36500", name="ck_organisations_run_retention_days"
        ),
        CheckConstraint(
            "chat_retention_days BETWEEN 1 AND 36500", name="ck_organisations_chat_retention_days"
        ),
        # The sweep's: those whose grace period is over.
        Index(
            "ix_organisations_deletes_at",
            "deletes_at",
            postgresql_where=text("deletes_at IS NOT NULL"),
        ),
    )


class OrganisationDeletion(Base):
    """The record of an organisation the sweep deleted once its grace period was over
    (`aios.organisations.delete_due`): what it was called, who scheduled it, when, and when it
    went. Kept after the organisation and its store are gone; nothing references the organisation
    (it no longer exists), and the store's row is kept as `deleted`."""

    __tablename__ = "organisation_deletions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The deleted organisation's id: not a foreign key, as it's gone.
    organisation_id: Mapped[int] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    # Null once that user is deleted.
    scheduled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The store detached from it for the teardown, if it had one.
    data_store_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_stores.id", ondelete="SET NULL")
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
    # The role whoever joins through it is given: editor unless the admin who made it chose.
    role: Mapped[Role] = mapped_column(ROLE_TYPE, server_default=Role.editor.value)
    # Set when an admin cuts the link off; a revoked link opens for no one.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganisationMember(Base):
    """A user's membership of an organisation: a user can be in any number of them."""

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
    # What they may do in it: its creator starts as its admin; whoever joins through a link, the
    # link's role.
    role: Mapped[Role] = mapped_column(ROLE_TYPE, server_default=Role.editor.value)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organisation: Mapped[Organisation] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()

    __table_args__ = (
        # For listing a person's organisations (`aios.organisations.memberships`).
        Index("ix_organisation_members_user_id", "user_id"),
    )
