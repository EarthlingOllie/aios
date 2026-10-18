"""schema history

The log of changes to the store's schema: approved schema change requests append to it as
they're applied.

Revision ID: 0001
Revises:
Create Date: 2026-10-15 23:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schema_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        # The schema change request it applied (in the platform database), if any.
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        # The statements that ran.
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="aios",
    )


def downgrade() -> None:
    op.drop_table("schema_history", schema="aios")
