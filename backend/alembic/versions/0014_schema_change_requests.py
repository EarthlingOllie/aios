"""schema change requests

Agents' requests to change their store's `data` schema, which the store's owner approves or
rejects (`aios.datastores.schema_changes`).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-17 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    "expired",
    "applied",
    "failed",
    "stale",
    "cancelled",
    name="schema_change_status",
)


def upgrade() -> None:
    op.create_table(
        "schema_change_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("data_store_id", sa.UUID(), nullable=False),
        sa.Column("ops", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by_run", sa.Integer(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", STATUS, server_default="pending", nullable=False),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["data_store_id"], ["data_stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_run"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("requested_by_run", "tool_call_id"),
    )
    op.create_index(
        op.f("ix_schema_change_requests_data_store_id"),
        "schema_change_requests",
        ["data_store_id"],
    )
    op.create_index(
        op.f("ix_schema_change_requests_requested_by_run"),
        "schema_change_requests",
        ["requested_by_run"],
    )


def downgrade() -> None:
    op.drop_table("schema_change_requests")
    STATUS.drop(op.get_bind())
