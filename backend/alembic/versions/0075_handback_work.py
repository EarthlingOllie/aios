"""handback work

The run service's queue gains the `handback` kind: summarising a run's result before it's
posted to a chat, in the interactive lane (`aios.run_service.work.handback`).

Revision ID: 0075
Revises: 0074
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "invocation_start",
    "agent_turn",
    "workflow_advance",
    "workflow_node",
    "resume_turn",
    "embedding_sweep",
)


def upgrade() -> None:
    op.execute("ALTER TYPE work_kind ADD VALUE IF NOT EXISTS 'handback'")


def downgrade() -> None:
    op.execute("DELETE FROM work_items WHERE kind::text = 'handback'")
    op.execute("ALTER TYPE work_kind RENAME TO work_kind_old")
    op.execute(f"CREATE TYPE work_kind AS ENUM ({', '.join(repr(k) for k in KINDS)})")
    op.execute(
        "ALTER TABLE work_items ALTER COLUMN kind TYPE work_kind USING kind::text::work_kind"
    )
    op.execute("DROP TYPE work_kind_old")
