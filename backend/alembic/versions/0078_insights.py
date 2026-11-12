"""insights: store usage samples and the per-person Insights setting

`store_usage_samples`: what each store used each time it was measured
(`aios.datastores.quota.measure`), for Insights' storage over time; deleted with the store,
pruned after `storage_history_days`.

Organisations gain `insights_people_visible`: whether Insights shows usage by person to every
member rather than admins only (off by default; admins switch it, audited).

Revision ID: 0078
Revises: 0077
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "store_usage_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "measured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("db_bytes", sa.BigInteger(), nullable=False),
        sa.Column("files_bytes", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_store_usage_samples_store_measured_at",
        "store_usage_samples",
        ["data_store_id", "measured_at"],
    )
    op.create_index("ix_store_usage_samples_measured_at", "store_usage_samples", ["measured_at"])
    op.add_column(
        "organisations",
        sa.Column(
            "insights_people_visible",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("organisations", "insights_people_visible")
    op.drop_index("ix_store_usage_samples_measured_at", "store_usage_samples")
    op.drop_index("ix_store_usage_samples_store_measured_at", "store_usage_samples")
    op.drop_table("store_usage_samples")
