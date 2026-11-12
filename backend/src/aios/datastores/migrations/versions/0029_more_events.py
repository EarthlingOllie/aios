"""more events: debounce batches, webhooks

The event type gains "webhook_received": a webhook trigger's public endpoint accepted a call.

Triggers gain a debounce window (`debounce_seconds`), and for webhook triggers the SHA-256 of
their endpoint's token (unique) and their signing secret, encrypted: both on the deny list.

`aios.event_batches`: a debounce batch, one open per trigger, with the ids of the events it has
gathered and a count of those left out past the cap.

`aios.webhook_deliveries`: the delivery ids each webhook trigger has accepted, for
de-duplication.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.event_type ADD VALUE IF NOT EXISTS 'webhook_received'")
    op.add_column(
        "triggers", sa.Column("debounce_seconds", sa.Integer(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "triggers", sa.Column("webhook_token_hash", sa.String(64), nullable=True), schema=SCHEMA
    )
    op.add_column("triggers", sa.Column("webhook_secret", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_index(
        "uq_triggers_webhook_token_hash",
        "triggers",
        ["webhook_token_hash"],
        unique=True,
        schema=SCHEMA,
    )

    op.create_table(
        "event_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "trigger_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.triggers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("dropped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
        comment="Debounce batches: events gathered for one firing of a trigger",
    )
    op.create_index(
        "uq_event_batches_trigger_id", "event_batches", ["trigger_id"], unique=True, schema=SCHEMA
    )
    op.create_index("ix_event_batches_closes_at", "event_batches", ["closes_at"], schema=SCHEMA)

    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "trigger_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.triggers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("delivery_id", sa.String(200), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=SCHEMA,
        comment="Webhook delivery ids seen, for de-duplication",
    )
    op.create_index(
        "ix_webhook_deliveries_received_at",
        "webhook_deliveries",
        ["received_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Postgres can't drop an enum value, so "webhook_received" stays, unused: no webhook
    trigger or event is left."""
    op.drop_table("webhook_deliveries", schema=SCHEMA)
    op.drop_table("event_batches", schema=SCHEMA)
    op.execute(f"DELETE FROM {SCHEMA}.events WHERE type::text = 'webhook_received'")
    op.execute(f"DELETE FROM {SCHEMA}.triggers WHERE event_type::text = 'webhook_received'")
    op.drop_index("uq_triggers_webhook_token_hash", "triggers", schema=SCHEMA)
    for column in ("webhook_secret", "webhook_token_hash", "debounce_seconds"):
        op.drop_column("triggers", column, schema=SCHEMA)
