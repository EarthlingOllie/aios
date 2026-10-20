"""history runs say which member started them

A run executes for its organisation, and records the member who started it, or none for a
schedule's run (`aios.api.agents.new_agent_run`). The exported copies said who ran them as the
agent's or workflow's owner, which was always someone; they now say who started the run, so
`user_id` is null for a schedule's run (and for a run from before runs recorded it).

Revision ID: h0004
Revises: h0003
Create Date: 2026-10-19 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h0004"
down_revision: str | None = "h0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "history"
TABLES = ("agent_runs", "workflow_runs")
COMMENT = "The member who started it; null for a schedule's run."


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(
            table,
            "user_id",
            existing_type=sa.BigInteger(),
            nullable=True,
            comment=COMMENT,
            existing_comment="The user who ran it.",
            schema=SCHEMA,
        )


def downgrade() -> None:
    # The copies without a member go, with their messages and steps: the release before can't
    # hold them. Turning the export off and on again copies them back, with their owner.
    for table in TABLES:
        op.execute(f"DELETE FROM {SCHEMA}.{table} WHERE user_id IS NULL")
        op.alter_column(
            table,
            "user_id",
            existing_type=sa.BigInteger(),
            nullable=False,
            comment="The user who ran it.",
            existing_comment=COMMENT,
            schema=SCHEMA,
        )
