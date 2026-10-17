"""notifications

In-app notifications for users (`aios.notifications`): to begin with, one for each user who can
decide a pending schema change request.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id_read_at", "notifications", ["user_id", "read_at"])
    op.create_index("ix_notifications_ref", "notifications", ["ref"], postgresql_using="gin")
    # Requests already waiting notify whoever can decide them (`resolve.approvers`: the store's
    # owner), as a new one would; else nothing would prompt their decision.
    op.execute(
        """
        INSERT INTO notifications (user_id, kind, ref, created_at)
        SELECT s.owner_user_id, 'schema_change_request',
               jsonb_build_object('request_id', r.id::text, 'data_store_id', r.data_store_id::text),
               r.created_at
        FROM schema_change_requests r JOIN data_stores s ON s.id = r.data_store_id
        WHERE r.status = 'pending'
        """
    )


def downgrade() -> None:
    op.drop_table("notifications")
