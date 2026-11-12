"""heartbeats gone

Every turn and workflow node runs on the run service (`aios.run_service`), whose leases and
retries replace the old heartbeats: `agent_runs.heartbeat_at` (and its index),
`invocations.heartbeat_at` and `workflow_runs.heartbeat_at` are dropped. A schedule's claim keeps
the time it was claimed, `triggers.heartbeat_at` renamed `claimed_at` (`aios.scheduler`).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
DROPPED = ("agent_runs", "invocations", "workflow_runs")


def upgrade() -> None:
    op.drop_index("ix_agent_runs_heartbeat", "agent_runs", schema=SCHEMA)
    for table in DROPPED:
        op.drop_column(table, "heartbeat_at", schema=SCHEMA)
    op.alter_column("triggers", "heartbeat_at", new_column_name="claimed_at", schema=SCHEMA)


def downgrade() -> None:
    op.alter_column("triggers", "claimed_at", new_column_name="heartbeat_at", schema=SCHEMA)
    for table in DROPPED:
        op.add_column(
            table,
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
    op.create_index(
        "ix_agent_runs_heartbeat",
        "agent_runs",
        ["heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("heartbeat_at IS NOT NULL"),
    )
