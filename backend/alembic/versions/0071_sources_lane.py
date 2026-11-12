"""the sources lane

The run service's queue gains the `sources` lane, for phase 4's syncs, beside interactive and
background (`aios.run_service.queue`).

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE work_lane ADD VALUE IF NOT EXISTS 'sources'")


def downgrade() -> None:
    op.execute("DELETE FROM work_items WHERE lane::text = 'sources'")
    op.execute("ALTER TYPE work_lane RENAME TO work_lane_old")
    op.execute("CREATE TYPE work_lane AS ENUM ('interactive', 'background')")
    op.execute(
        "ALTER TABLE work_items ALTER COLUMN lane TYPE work_lane USING lane::text::work_lane"
    )
    op.execute("DROP TYPE work_lane_old")
