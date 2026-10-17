"""data stores

A data store is where a user's data lives (`aios.datastores`). Each user has one, with an id of
its own. For now every store is in the platform database, so this only adds the rows; existing
users get theirs here, new users when they sign in.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15 21:55:18.847559
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
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
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
