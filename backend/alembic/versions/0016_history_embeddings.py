"""history embeddings

Searchable run history (`aios.datastores.history`): the per-user setting that embeds the
exported messages, and on each data store when embedding them last caught up.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "embed_run_history", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "data_stores",
        sa.Column("history_embedded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_stores", "history_embedded_at")
    op.drop_column("users", "embed_run_history")
