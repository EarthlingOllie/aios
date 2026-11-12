"""source tokens: webhook sources' tokens and email sources' addresses

A webhook token (`webhook_tokens`) now leads to a trigger or to a webhook source
(`source_id`): exactly one of the two. `inbound_addresses`: where each email source's address
leads, by the address's SHA-256 (the address itself isn't kept), so inbound mail
(`aios.api.inbound_mail`) finds the store and source.

Revision ID: 0082
Revises: 0081
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("webhook_tokens", "trigger_id", nullable=True)
    op.add_column("webhook_tokens", sa.Column("source_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_webhook_tokens_one_target",
        "webhook_tokens",
        "(trigger_id IS NULL) <> (source_id IS NULL)",
    )
    op.create_table(
        "inbound_addresses",
        sa.Column("address_hash", sa.String(64), primary_key=True),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_inbound_addresses_data_store_id", "inbound_addresses", ["data_store_id"])
    op.create_index(
        "ix_inbound_addresses_organisation_id", "inbound_addresses", ["organisation_id"]
    )


def downgrade() -> None:
    op.drop_table("inbound_addresses")
    op.execute("DELETE FROM webhook_tokens WHERE trigger_id IS NULL")
    op.drop_constraint("ck_webhook_tokens_one_target", "webhook_tokens", type_="check")
    op.drop_column("webhook_tokens", "source_id")
    op.alter_column("webhook_tokens", "trigger_id", nullable=False)
