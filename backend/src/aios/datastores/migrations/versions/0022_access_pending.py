"""access pending

`aios.table_owners.access_pending_since`: when a change to who may write the table (an owner
set, a share added or removed, an owner or sharer archived) was made that the store's grants
don't carry yet (`aios.datastores.agent_roles`); null once they do.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "table_owners",
        sa.Column(
            "access_pending_since",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a change to who may write it was made that the grants don't carry"
            " yet; null: they do.",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("table_owners", "access_pending_since", schema=SCHEMA)
