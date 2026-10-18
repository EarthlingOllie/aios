"""builtin agents

An agent can be one of the app's built-in agents, made for its owner by the app (the documents
assistant, which joins each new chat): `builtin` names which, and an owner has at most one live
agent of each.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-18 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("builtin", sa.String(length=32), nullable=True))
    op.create_index(
        "uq_agents_owner_id_builtin_live",
        "agents",
        ["owner_id", "builtin"],
        unique=True,
        postgresql_where=sa.text("builtin IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agents_owner_id_builtin_live", table_name="agents")
    op.drop_column("agents", "builtin")
