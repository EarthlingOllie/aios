"""chat invocation heartbeat

An agent invocation's turn runs in an API process (`aios.firing`), which claims it when it has a
free slot under its cap and refreshes this while the turn runs; one whose heartbeat has stopped
lost its process and is failed. Null while it waits for a slot, and for a workflow's (the worker
looks after its run).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_invocations", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("chat_invocations", "heartbeat_at")
