"""agent runs on LangGraph: history moves to the checkpointer

Existing runs are deleted: their history is in Pydantic AI's message format, which nothing
reads any more. Their sandbox session rows go with them (ON DELETE CASCADE); the sandboxes stop
themselves once idle.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM agent_runs")
    op.drop_column("agent_runs", "messages")
    op.add_column(
        "agent_runs",
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_runs")
    op.drop_column("agent_runs", "message_count")
    op.add_column(
        "agent_runs",
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    # The checkpointer's threads are keyed by run id, so they must go with the runs: a later
    # run that reuses an id (after downgrading past `agent_runs`) would otherwise inherit one.
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
