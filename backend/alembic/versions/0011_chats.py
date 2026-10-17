"""chats

Chats are group conversations. A chat has an owner, a name unique among chats, a description and
a visibility (a private chat doesn't exist for users who aren't in it). Its members are users,
agents or workflows, one of which each member row points at (`ck_chat_members_target`, like a
trigger's target); only users have a role (owner or member). Only user members are added so far.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17 13:50:31.354629
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "visibility", sa.Enum("public", "private", name="chat_visibility"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_chats_owner_id"), "chats", ["owner_id"], unique=False)
    op.create_table(
        "chat_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind", sa.Enum("user", "agent", "workflow", name="chat_member_kind"), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.Enum("owner", "member", name="chat_member_role"), nullable=True),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL AND workflow_id IS NULL)"
            " OR (kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL"
            " AND workflow_id IS NULL)"
            " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND user_id IS NULL"
            " AND agent_id IS NULL)",
            name="ck_chat_members_target",
        ),
        sa.CheckConstraint("(kind = 'user') = (role IS NOT NULL)", name="ck_chat_members_role"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "agent_id"),
        sa.UniqueConstraint("chat_id", "user_id"),
        sa.UniqueConstraint("chat_id", "workflow_id"),
    )
    op.create_index(op.f("ix_chat_members_agent_id"), "chat_members", ["agent_id"], unique=False)
    op.create_index(op.f("ix_chat_members_chat_id"), "chat_members", ["chat_id"], unique=False)
    op.create_index(op.f("ix_chat_members_user_id"), "chat_members", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_chat_members_workflow_id"), "chat_members", ["workflow_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_members_workflow_id"), table_name="chat_members")
    op.drop_index(op.f("ix_chat_members_user_id"), table_name="chat_members")
    op.drop_index(op.f("ix_chat_members_chat_id"), table_name="chat_members")
    op.drop_index(op.f("ix_chat_members_agent_id"), table_name="chat_members")
    op.drop_table("chat_members")
    op.drop_index(op.f("ix_chats_owner_id"), table_name="chats")
    op.drop_table("chats")
    for name in ("chat_member_role", "chat_member_kind", "chat_visibility"):
        sa.Enum(name=name).drop(op.get_bind())
