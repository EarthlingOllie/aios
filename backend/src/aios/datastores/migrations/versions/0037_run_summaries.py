"""run summaries: compaction of long conversations

`aios.run_summaries`: summaries of agent runs' oldest turns, which their models see in their
place (`aios.compaction`), each with the last turn it covers and the first message it doesn't.
Derived text (verdict `derived`).

Turns gain `message_end`: how many messages their run's conversation had once they ended, which
places each turn's messages in it (null for turns before this).

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
VERDICTS = ("awaiting_check", "clean", "flagged", "released", "derived")


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column(
            "message_end",
            sa.Integer(),
            nullable=True,
            comment="How many messages its run's conversation had once it ended.",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "run_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "last_turn_id",
            sa.Integer(),
            nullable=True,
            comment="The last turn it covers; null if that couldn't be told.",
        ),
        sa.Column(
            "kept_from_message_id",
            sa.Text(),
            nullable=False,
            comment="The id of the first message of the run's history it doesn't cover.",
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum(*VERDICTS, name="verdict", schema=SCHEMA, create_type=False),
            nullable=False,
            server_default="derived",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema=SCHEMA,
        comment="Summaries of agent runs' oldest turns, which their models see instead",
    )
    op.create_index(
        "ix_run_summaries_agent_run_id_id",
        "run_summaries",
        ["agent_run_id", "id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("run_summaries", schema=SCHEMA)
    op.drop_column("turns", "message_end", schema=SCHEMA)
