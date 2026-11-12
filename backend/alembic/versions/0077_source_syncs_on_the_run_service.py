"""source syncs on the run service

The run service's queue gains the `source_sync` kind: one sync of a source (`aios.sync`), in the
`sources` lane, one queued per source at a time (`aios.run_service.work.source_sync`).

Revision ID: 0077
Revises: 0076
Create Date: 2026-11-21 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "invocation_start",
    "agent_turn",
    "workflow_advance",
    "workflow_node",
    "resume_turn",
    "embedding_sweep",
    "handback",
)


def upgrade() -> None:
    op.execute("ALTER TYPE work_kind ADD VALUE IF NOT EXISTS 'source_sync'")


def downgrade() -> None:
    op.execute("DELETE FROM work_items WHERE kind::text = 'source_sync'")
    op.execute("ALTER TYPE work_kind RENAME TO work_kind_old")
    op.execute(f"CREATE TYPE work_kind AS ENUM ({', '.join(repr(k) for k in KINDS)})")
    op.execute(
        "ALTER TABLE work_items ALTER COLUMN kind TYPE work_kind USING kind::text::work_kind"
    )
    op.execute("DROP TYPE work_kind_old")
