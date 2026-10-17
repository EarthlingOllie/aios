"""data store usage

What each data store uses, as last measured on a schedule (`aios.datastores.quota`): its
database's size and its documents' files' bytes, and when. NULL until it's first measured.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-17 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("data_stores", sa.Column("db_bytes", sa.BigInteger(), nullable=True))
    op.add_column("data_stores", sa.Column("files_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "data_stores",
        sa.Column("usage_measured_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_stores", "usage_measured_at")
    op.drop_column("data_stores", "files_bytes")
    op.drop_column("data_stores", "db_bytes")
