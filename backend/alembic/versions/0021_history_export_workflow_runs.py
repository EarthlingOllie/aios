"""history export workflow runs

The run history export also copies workflow runs (`aios.datastores.history`): their rows in its
outbox (`history_exports`) are of a kind of their own.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Not usable in the transaction that adds it, which nothing here needs.
    op.execute("ALTER TYPE history_export_kind ADD VALUE IF NOT EXISTS 'workflow_run'")


def downgrade() -> None:
    # Postgres can't drop an enum's value: the type is made again without it.
    op.execute("DELETE FROM history_exports WHERE kind = 'workflow_run'")
    op.execute("ALTER TYPE history_export_kind RENAME TO history_export_kind_old")
    op.execute("CREATE TYPE history_export_kind AS ENUM ('agent_run')")
    op.execute(
        "ALTER TABLE history_exports ALTER COLUMN kind TYPE history_export_kind"
        " USING kind::text::history_export_kind"
    )
    op.execute("DROP TYPE history_export_kind_old")
