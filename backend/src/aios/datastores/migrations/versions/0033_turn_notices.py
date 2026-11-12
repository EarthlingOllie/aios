"""turn notices

Turns gain `notices`: what their run was told about tools left out of the turn (a connector's
tools that are unavailable, a pinned tool that vanished upstream: `aios.tools.connector`), shown
with the run's turns.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column(
            "notices",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="What the run was told about tools left out of the turn, in order.",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("turns", "notices", schema=SCHEMA)
