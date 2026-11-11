"""Chats: chats, their members, messages and invocations."""

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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aios.models.base import Base
from aios.models.identity import User
from aios.models.triggers import TriggerTargetKind

# Chats are group conversations between users, agents and workflows. A member row says who is
# in a chat; which kind it is decides which id column is set, like a trigger's target, so agents
# and workflows can be members as well as users. Only users have a role: the owner (one per chat:
# the user who made it, or whom it was handed to when they left the organisation) can change the
# chat, remove members and delete it.


class ChatVisibility(enum.StrEnum):
    public = "public"
    private = "private"


class ChatMemberKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"


class ChatMemberRole(enum.StrEnum):
    owner = "owner"
    member = "member"


class Chat(Base):
    """A group conversation. Its members are `ChatMember` rows, its messages `ChatMessage`s."""

    __tablename__ = "chats"
    __table_args__ = (
        # Unique within the organisation: a chat is named like a channel, not like one member's
        # own thing, and two organisations can each have "general".
        Index("uq_chats_organisation_id_name", "organisation_id", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The organisation it belongs to (`aios.organisations`): the chat exists only for its
    # members, and goes with it. Who owns the chat is a role of its members (`ChatMember.role`).
    organisation_id: Mapped[int] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    # Which member made it and which last changed it; null once that user is deleted.
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    # A private chat doesn't exist for a user who isn't a member of it.
    visibility: Mapped[ChatVisibility] = mapped_column(Enum(ChatVisibility, name="chat_visibility"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When it was last active: when it was last changed, or a message was last posted.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list["ChatMember"]] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMember.id",
    )


class ChatMember(Base):
    """Who is in a chat: a user, an agent or a workflow (all of the chat's organisation)."""

    __tablename__ = "chat_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    kind: Mapped[ChatMemberKind] = mapped_column(Enum(ChatMemberKind, name="chat_member_kind"))
    # Exactly one is set, matching `kind`. The cascade is for deleting a user. An agent or
    # workflow is the one in the chat's organisation's store (`aios.datastores.models`), so not
    # a foreign key: they're archived, never deleted.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[int | None] = mapped_column(Integer, index=True)
    workflow_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Users only: the chat's owner, or an ordinary member.
    role: Mapped[ChatMemberRole | None] = mapped_column(
        Enum(ChatMemberRole, name="chat_member_role")
    )
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Users only: the id of the last of the chat's messages they've seen (null: none yet), from
    # which their unread count is counted. A cursor, not a reference: it only goes up.
    last_read_message_id: Mapped[int | None]

    chat: Mapped[Chat] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint(
            "(kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL AND workflow_id IS NULL)"
            " OR (kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL"
            " AND workflow_id IS NULL)"
            " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND user_id IS NULL"
            " AND agent_id IS NULL)",
            name="ck_chat_members_target",
        ),
        CheckConstraint(
            "(kind = 'user') = (role IS NOT NULL)",
            name="ck_chat_members_role",
        ),
        UniqueConstraint("chat_id", "user_id"),
        UniqueConstraint("chat_id", "agent_id"),
        UniqueConstraint("chat_id", "workflow_id"),
        # One owner at most: none only while nobody is left in it to hand the chat to.
        Index(
            "uq_chat_members_chat_id_owner",
            "chat_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )


class ChatAuthorKind(enum.StrEnum):
    user = "user"
    agent = "agent"
    workflow = "workflow"
    # The chat itself (no author), for messages about it.
    system = "system"


class ChatMessageKind(enum.StrEnum):
    # Markdown, as written.
    text = "text"
    # An acknowledgement from the agent or workflow a mention started, with a card under it
    # following the run (`invocation_id`).
    run_card = "run_card"


class ChatMessage(Base):
    """A message in a chat, read in id order: `?after=` and `?before=` page by id."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    author_kind: Mapped[ChatAuthorKind] = mapped_column(
        Enum(ChatAuthorKind, name="chat_author_kind")
    )
    # At most one is set, matching `author_kind`; none for a system message. Deleting the user
    # leaves what they posted, with no author. An agent or workflow is the one in the chat's
    # organisation's store, so not a foreign key.
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    author_agent_id: Mapped[int | None] = mapped_column(Integer, index=True)
    author_workflow_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[ChatMessageKind] = mapped_column(Enum(ChatMessageKind, name="chat_message_kind"))
    body: Mapped[str] = mapped_column(Text)
    # Who the message mentions, `[{kind, id, name}]`, with the names as they were when posted.
    mentions: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The invocation it's part of: a run card's, or the result the run posted back. Deleted with
    # the chat, as the invocation is.
    invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "chat_invocations.id",
            ondelete="CASCADE",
            use_alter=True,
            name="fk_chat_messages_invocation_id",
        ),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    author_user: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(author_kind = 'user' OR author_user_id IS NULL)"
            " AND (author_kind = 'agent' OR author_agent_id IS NULL)"
            " AND (author_kind = 'workflow' OR author_workflow_id IS NULL)",
            name="ck_chat_messages_author",
        ),
        CheckConstraint(
            "kind <> 'run_card' OR invocation_id IS NOT NULL", name="ck_chat_messages_run_card"
        ),
        # A chat's messages in order, for the cursor reads.
        Index("ix_chat_messages_chat_id_id", "chat_id", "id"),
    )


class ChatInvocationStatus(enum.StrEnum):
    # Waiting for its run to start (`aios.invocations`).
    queued = "queued"
    # The handoff is briefing its run on the chat (`aios.handoff`); the run starts next.
    briefing = "briefing"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"

    @property
    def finished(self) -> bool:
        return self in (ChatInvocationStatus.succeeded, ChatInvocationStatus.failed)


class ChatInvocation(Base):
    """A run of an agent or workflow that a mention in a chat started (`aios.invocations`): one
    per mention, shaped like a trigger's outcome but one event rather than a standing rule. Its
    card follows the run; when the run ends, its result is posted back and it finishes."""

    __tablename__ = "chat_invocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    # The message that mentioned it.
    message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    # Who mentioned it: the member who started its run (the run's `created_by`).
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    target_kind: Mapped[TriggerTargetKind] = mapped_column(
        Enum(TriggerTargetKind, name="chat_invocation_target_kind")
    )
    # Exactly one is set, matching `target_kind`: the agent or workflow in the chat's
    # organisation's store (`aios.datastores.models`), so not a foreign key.
    agent_id: Mapped[int | None] = mapped_column(Integer, index=True)
    workflow_id: Mapped[int | None] = mapped_column(Integer, index=True)
    status: Mapped[ChatInvocationStatus] = mapped_column(
        Enum(ChatInvocationStatus, name="chat_invocation_status"),
        default=ChatInvocationStatus.queued,
    )
    # Why it failed: the run's error, or why it couldn't start.
    error: Mapped[str | None] = mapped_column(Text)
    # What the handoff told its run of the chat (`aios.handoff`); null until it has.
    brief: Mapped[str | None] = mapped_column(Text)
    # The run it started; null until it has, and once that run has been deleted (the card says
    # so). Deleting the chat leaves the run.
    # By id in the chat's organisation's store (not foreign keys): cleared when the run is
    # deleted (`aios.invocations.forget_runs`).
    agent_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    workflow_run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When its run started (null if it never did), and when it finished.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # An agent's: set when a process claims it to run its turn (`aios.firing`) and refreshed
    # while the turn runs; one whose heartbeat has stopped lost its process, and is failed. Null
    # while it waits for a free slot, and for a workflow's, whose run the worker looks after.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_chat_invocations_target",
        ),
        # A chat's unfinished invocations: what its poll reports on.
        Index(
            "ix_chat_invocations_unfinished",
            "chat_id",
            postgresql_where=text("status IN ('queued', 'briefing', 'running')"),
        ),
    )
