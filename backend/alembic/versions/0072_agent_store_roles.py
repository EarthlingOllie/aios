"""agent store roles

Each agent that writes gets its own login role in its organisation's store
(`aios.datastores.agent_roles`): this records its name and encrypted password.

Revision ID: 0072
Revises: 0071
Create Date: 2026-11-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_store_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("data_store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("role_name", sa.String(length=63), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["data_store_id"], ["data_stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role_name"),
    )
    op.create_index(
        "uq_agent_store_roles_store_agent",
        "agent_store_roles",
        ["data_store_id", "agent_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_agent_store_roles_store_agent", table_name="agent_store_roles")
    op.drop_table("agent_store_roles")
