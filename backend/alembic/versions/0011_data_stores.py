"""data stores

A data store is where a user's data lives (their documents and chunks), with its own id so that a
team can own one later. For now each points at the platform database. Every existing user gets
one; new users get theirs on sign-in (`aios.datastores.data_store_for`).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 20:32:19.073263
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
        "data_stores",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id"),
    )
    op.execute("INSERT INTO data_stores (owner_user_id) SELECT id FROM users")


def downgrade() -> None:
    op.drop_table("data_stores")
