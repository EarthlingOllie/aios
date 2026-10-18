"""chat invocations

A run a mention in a chat started (`chat_invocations`): the chat, the mentioning message, who
asked, the agent or workflow (exactly one, as a trigger's target), how it's going, why it failed,
the run it started (null once that run is deleted, so the card can say so) and when. A message
can belong to one: a run card, or the result posted back.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-18 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_invocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column(
            "target_kind",
            sa.Enum("agent", "workflow", name="chat_invocation_target_kind"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "succeeded", "failed", name="chat_invocation_status"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("workflow_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_chat_invocations_target",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id"),
        sa.UniqueConstraint("workflow_run_id"),
    )
    for column in ("chat_id", "message_id", "requested_by", "agent_id", "workflow_id"):
        op.create_index(
            op.f(f"ix_chat_invocations_{column}"), "chat_invocations", [column], unique=False
        )
    op.create_index(
        "ix_chat_invocations_unfinished",
        "chat_invocations",
        ["chat_id"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.add_column("chat_messages", sa.Column("invocation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_chat_messages_invocation_id",
        "chat_messages",
        "chat_invocations",
        ["invocation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_chat_messages_invocation_id"), "chat_messages", ["invocation_id"], unique=False
    )
    op.create_check_constraint(
        "ck_chat_messages_run_card",
        "chat_messages",
        "kind <> 'run_card' OR invocation_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_chat_messages_run_card", "chat_messages", type_="check")
    op.drop_index(op.f("ix_chat_messages_invocation_id"), table_name="chat_messages")
    op.drop_constraint("fk_chat_messages_invocation_id", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "invocation_id")
    op.drop_index("ix_chat_invocations_unfinished", table_name="chat_invocations")
    for column in ("workflow_id", "agent_id", "requested_by", "message_id", "chat_id"):
        op.drop_index(op.f(f"ix_chat_invocations_{column}"), table_name="chat_invocations")
    op.drop_table("chat_invocations")
    for name in ("chat_invocation_status", "chat_invocation_target_kind"):
        sa.Enum(name=name).drop(op.get_bind())
