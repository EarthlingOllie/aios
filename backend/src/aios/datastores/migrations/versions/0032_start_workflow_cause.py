"""start_workflow: the invocation cause for a workflow an agent started

Invocations gain the cause "start_workflow": a workflow run an agent started with its
`start_workflow` tool (`aios.tools.start_workflow`), chained to the calling turn's invocation.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.invocation_cause ADD VALUE IF NOT EXISTS 'start_workflow'")


def downgrade() -> None:
    """Postgres can't drop an enum value, and views and the usage ledger use the type, so the
    value stays: invocations an agent started become started by hand (the usage ledger, kept
    as written, still names it)."""
    op.execute(
        f"UPDATE {SCHEMA}.invocations SET cause = 'manual' WHERE cause::text = 'start_workflow'"
    )
