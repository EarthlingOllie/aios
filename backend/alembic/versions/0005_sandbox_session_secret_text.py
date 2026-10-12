"""sandbox session secret as text

The Lambda backend keeps each session's proxy URL as its secret, and the proxy token in it grows
with the run's allowed domains.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sandbox_sessions",
        "secret",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # A session whose secret no longer fits is dropped: its run starts a new sandbox.
    op.execute("DELETE FROM sandbox_sessions WHERE length(secret) > 255")
    op.alter_column(
        "sandbox_sessions",
        "secret",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
