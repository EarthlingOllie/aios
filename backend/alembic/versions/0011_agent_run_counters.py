"""agent run counters

Agent runs record how they went and what they cost: a status (running, succeeded, failed), when
their latest turn began and ended, and, over all their turns, the model's tool calls and the
input and output tokens of each model call, written by the turn loop at each model step. Null for
runs from before, which are shown as not tracked. A heartbeat marks a run whose turn is running
outside a request (`aios.firing`), so one whose process died can be failed.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17 10:12:41.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = sa.Enum("running", "succeeded", "failed", name="agent_run_status")


def upgrade() -> None:
    STATUS.create(op.get_bind())
    op.add_column(
        "agent_runs",
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_run_status", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("agent_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_runs", sa.Column("tool_call_count", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_agent_runs_running_heartbeat",
        "agent_runs",
        ["heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running' AND heartbeat_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_runs_running_heartbeat",
        table_name="agent_runs",
        postgresql_where=sa.text("status = 'running' AND heartbeat_at IS NOT NULL"),
    )
    for name in (
        "heartbeat_at",
        "output_tokens",
        "input_tokens",
        "tool_call_count",
        "finished_at",
        "started_at",
        "status",
    ):
        op.drop_column("agent_runs", name)
    STATUS.drop(op.get_bind())
