"""workflows on the run service

Workflow runs advance node by node on the run service (`aios.workflows.advance`): the queue
gains the workflow advance, workflow node and resume turn kinds, and `work_items.again`, set when
an item is enqueued again while it runs, so it runs once more rather than missing what changed.

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

KINDS = ("workflow_advance", "workflow_node", "resume_turn")


def upgrade() -> None:
    for kind in KINDS:
        op.execute(f"ALTER TYPE work_kind ADD VALUE IF NOT EXISTS '{kind}'")
    op.add_column(
        "work_items",
        sa.Column("again", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("work_items", "again")
    op.execute(f"DELETE FROM work_items WHERE kind::text IN {KINDS!r}")
    op.execute("ALTER TYPE work_kind RENAME TO work_kind_old")
    op.execute("CREATE TYPE work_kind AS ENUM ('invocation_start', 'agent_turn')")
    op.execute(
        "ALTER TABLE work_items ALTER COLUMN kind TYPE work_kind USING kind::text::work_kind"
    )
    op.execute("DROP TYPE work_kind_old")
