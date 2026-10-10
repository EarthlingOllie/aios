"""agent version tools

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 22:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_version_tools",
        sa.Column("agent_version_id", sa.Integer(), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_version_id", "tool_key"),
    )


def downgrade() -> None:
    op.drop_table("agent_version_tools")
