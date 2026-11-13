"""insight charts: charts saved to the Insights page, and chart questions as invocations

`insight_charts`: each chart a member saved for the organisation (`aios.charts`): its title,
the question it answered, its one SQL statement and its Vega-Lite spec, who saved it, and when.
Viewing it re-runs its SQL as the store's insights role, never the agent.

Asking for a chart is an invocation of the built-in insights agent, of the new cause
`insight`, charged to whoever asked.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.invocation_cause ADD VALUE IF NOT EXISTS 'insight'")
    op.create_table(
        "insight_charts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=SCHEMA,
        comment="Charts saved to the Insights page",
    )


def downgrade() -> None:
    op.drop_table("insight_charts", schema=SCHEMA)
    # Postgres can't drop an enum value: `insight` stays, unused.
