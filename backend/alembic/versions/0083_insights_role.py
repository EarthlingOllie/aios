"""insights role: each store's password for its insights login role

Prompted charts (`aios.charts`) read a store as its own login role, `<prefix>_insights`, which
reads only the store's `insights` views and its `data` tables
(`aios.datastores.provision`). Its password is kept encrypted with the other roles'; stores
made before it get one the next time they're migrated.

Revision ID: 0083
Revises: 0082
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("data_stores", sa.Column("insights_password", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_stores", "insights_password")
