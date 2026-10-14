"""workflow layout

Where the editor draws each node of a workflow, by node key. Presentation only, like the name:
it's kept on the workflow, not its versions, so moving nodes doesn't create a version.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column(
            "layout",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("workflows", "layout")
