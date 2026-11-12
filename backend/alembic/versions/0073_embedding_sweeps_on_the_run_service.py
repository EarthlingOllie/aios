"""embedding sweeps on the run service

The run service's queue gains the `embedding_sweep` kind: embedding a store's search items
waiting for an embedding, in the background lane, which the routine worker's beat and the ingest
queue did before (`aios.run_service.work.embedding_sweep`).

Revision ID: 0073
Revises: 0072
Create Date: 2026-11-19 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("invocation_start", "agent_turn", "workflow_advance", "workflow_node", "resume_turn")


def upgrade() -> None:
    op.execute("ALTER TYPE work_kind ADD VALUE IF NOT EXISTS 'embedding_sweep'")


def downgrade() -> None:
    op.execute("DELETE FROM work_items WHERE kind::text = 'embedding_sweep'")
    op.execute("ALTER TYPE work_kind RENAME TO work_kind_old")
    op.execute(f"CREATE TYPE work_kind AS ENUM ({', '.join(repr(k) for k in KINDS)})")
    op.execute(
        "ALTER TABLE work_items ALTER COLUMN kind TYPE work_kind USING kind::text::work_kind"
    )
    op.execute("DROP TYPE work_kind_old")
