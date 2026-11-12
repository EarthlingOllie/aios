"""event triggers: the outbox, and the trigger and invocation fields they need

`aios.events`, the outbox (`aios.events`): what happened in the store (a document ingested, a
data table written, a schema change applied, a chat message posted), written in the same
transaction as the change, with its causing invocation and that invocation's chain of causes,
root first. The dispatcher marks each dispatched; dispatched ones are pruned after a while.

Triggers gain the kind "event", with an event type and a filter, a "fire on own events" flag
and a disabled reason; a constraint requires an event type for event triggers. A refused
firing is recorded as the trigger's last status: "refused", or "rate_limited".

Invocations gain the cause "event", and the event that caused one.

The new enum values can't be used in the transaction that adds them, so the constraint compares
the kind as text.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
EVENT_TYPES = ("document_ingested", "table_written", "schema_change_applied", "chat_message")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"ALTER TYPE {SCHEMA}.trigger_kind ADD VALUE IF NOT EXISTS 'event'")
    op.execute(f"ALTER TYPE {SCHEMA}.trigger_status ADD VALUE IF NOT EXISTS 'refused'")
    op.execute(f"ALTER TYPE {SCHEMA}.trigger_status ADD VALUE IF NOT EXISTS 'rate_limited'")
    op.execute(f"ALTER TYPE {SCHEMA}.invocation_cause ADD VALUE IF NOT EXISTS 'event'")
    sa.Enum(*EVENT_TYPES, name="event_type", schema=SCHEMA).create(bind)
    event_type = postgresql.ENUM(name="event_type", schema=SCHEMA, create_type=False)

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("type", event_type, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.invocations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "chain",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
        comment="The outbox: what happened in the store, for event triggers",
    )
    op.create_index(
        "ix_events_undispatched",
        "events",
        ["id"],
        schema=SCHEMA,
        postgresql_where=sa.text("dispatched_at IS NULL"),
    )
    op.create_index(
        "ix_events_dispatched_at",
        "events",
        ["dispatched_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("dispatched_at IS NOT NULL"),
    )

    op.add_column("triggers", sa.Column("event_type", event_type, nullable=True), schema=SCHEMA)
    op.add_column(
        "triggers",
        sa.Column(
            "filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "triggers",
        sa.Column(
            "fire_on_own_events", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        schema=SCHEMA,
    )
    op.add_column("triggers", sa.Column("disabled_reason", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_check_constraint(
        "ck_triggers_event",
        "triggers",
        "kind::text <> 'event' OR event_type IS NOT NULL",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_triggers_event_type",
        "triggers",
        ["event_type"],
        schema=SCHEMA,
        postgresql_where=sa.text("event_type IS NOT NULL"),
    )

    op.add_column(
        "invocations",
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey(
                f"{SCHEMA}.events.id", ondelete="SET NULL", name="fk_invocations_event_id"
            ),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_invocations_event_id", "invocations", ["event_id"], schema=SCHEMA)


def downgrade() -> None:
    """Postgres can't drop an enum value, and views and the usage ledger use these types, so the
    values added stay, unused: no event trigger, refused status or event cause is left."""
    op.drop_index("ix_invocations_event_id", "invocations", schema=SCHEMA)
    op.drop_column("invocations", "event_id", schema=SCHEMA)
    op.execute(f"DELETE FROM {SCHEMA}.triggers WHERE kind::text = 'event'")
    op.execute(
        f"UPDATE {SCHEMA}.triggers SET last_status = 'failed'"
        " WHERE last_status::text IN ('refused', 'rate_limited')"
    )
    op.execute(f"UPDATE {SCHEMA}.invocations SET cause = 'manual' WHERE cause::text = 'event'")
    op.drop_index("ix_triggers_event_type", "triggers", schema=SCHEMA)
    op.drop_constraint("ck_triggers_event", "triggers", schema=SCHEMA)
    for column in ("disabled_reason", "fire_on_own_events", "filter", "event_type"):
        op.drop_column("triggers", column, schema=SCHEMA)
    op.drop_table("events", schema=SCHEMA)
    sa.Enum(name="event_type", schema=SCHEMA).drop(op.get_bind())
