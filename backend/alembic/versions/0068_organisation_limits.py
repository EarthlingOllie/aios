"""organisation limits

An organisation's caps on turns and workflow runs (`aios.limits`), where an admin overrode the
deployment's defaults: a JSON object by cap name, empty for all the defaults.

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-15 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column(
            "limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("organisations", "limits")
