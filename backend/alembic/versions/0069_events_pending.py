"""events pending

When a store's outbox last got events the dispatcher hasn't seen (`aios.events`): stamped by
the writer once the store has committed them, cleared by the dispatcher as it claims the store.
Null: nothing pending. The event sweep stamps a store whose stamp was lost.

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_stores", sa.Column("events_pending_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_data_stores_events_pending_at",
        "data_stores",
        ["events_pending_at"],
        postgresql_where=sa.text("events_pending_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_data_stores_events_pending_at", "data_stores")
    op.drop_column("data_stores", "events_pending_at")
