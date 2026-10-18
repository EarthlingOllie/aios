"""chat messages

A chat's messages: who posted it (a user, agent or workflow, or the chat itself; the author's
id goes null if they're deleted, so the message stays), its kind (markdown text, or later a
run's card), its body and the members it mentions (empty for now). Read by id within a chat
(`?after=` for what's new, `?before=` for history), which the (chat_id, id) index serves.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-18 09:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column(
            "author_kind",
            sa.Enum("user", "agent", "workflow", "system", name="chat_author_kind"),
            nullable=False,
        ),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_agent_id", sa.Integer(), nullable=True),
        sa.Column("author_workflow_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Enum("text", "run_card", name="chat_message_kind"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(author_kind = 'user' OR author_user_id IS NULL)"
            " AND (author_kind = 'agent' OR author_agent_id IS NULL)"
            " AND (author_kind = 'workflow' OR author_workflow_id IS NULL)",
            name="ck_chat_messages_author",
        ),
        sa.ForeignKeyConstraint(["author_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_workflow_id"], ["workflows.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_chat_id_id", "chat_messages", ["chat_id", "id"], unique=False)
    op.create_index(
        op.f("ix_chat_messages_author_user_id"), "chat_messages", ["author_user_id"], unique=False
    )
    op.create_index(
        op.f("ix_chat_messages_author_agent_id"), "chat_messages", ["author_agent_id"], unique=False
    )
    op.create_index(
        op.f("ix_chat_messages_author_workflow_id"),
        "chat_messages",
        ["author_workflow_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_messages_author_workflow_id"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_author_agent_id"), table_name="chat_messages")
    op.drop_index(op.f("ix_chat_messages_author_user_id"), table_name="chat_messages")
    op.drop_index("ix_chat_messages_chat_id_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    for name in ("chat_message_kind", "chat_author_kind"):
        sa.Enum(name=name).drop(op.get_bind())
