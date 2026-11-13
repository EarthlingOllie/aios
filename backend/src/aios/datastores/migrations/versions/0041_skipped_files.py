"""source syncs: the files each left out

A sync record lists the files its sync skipped (unsupported, or over the provider's file cap),
each with why: `skipped_files`, `[{"name", "reason"}]`, the first 200 (`dropped` counts them all).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "source_syncs",
        sa.Column(
            "skipped_files",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="The files it left out (unsupported, or too big), with why: the first 200.",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("source_syncs", "skipped_files", schema=SCHEMA)
