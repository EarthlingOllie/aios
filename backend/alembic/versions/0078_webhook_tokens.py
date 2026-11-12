"""webhook tokens

Where each webhook trigger's token leads (`webhook_tokens`): by the token's SHA-256, the store
and trigger it belongs to, and their organisation, so the public endpoint (`aios.api.webhooks`)
finds them without the organisation in its path. The token itself isn't kept.

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
        "webhook_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger_id", sa.Integer(), nullable=False),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index("ix_webhook_tokens_data_store_id", "webhook_tokens", ["data_store_id"])
    op.create_index("ix_webhook_tokens_organisation_id", "webhook_tokens", ["organisation_id"])


def downgrade() -> None:
    op.drop_table("webhook_tokens")
