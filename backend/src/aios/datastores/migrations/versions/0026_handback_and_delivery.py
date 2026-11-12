"""handback and delivery

Results that come back to a conversation are summarised by a handback before they're posted
(`aios.handback`), and schedules can deliver their results to a chat.

- `invocations` gains the "handing back" status (its run has ended; the handback is summarising
  its output), the full output, the summary, why the handback failed (`handback_error`), the
  chat a trigger's run delivers to (`delivery_chat_id`) and why it couldn't
  (`delivery_error`).
- `triggers` gains its destination (`destination_chat_id`, cleared if the chat goes) and who
  set it (`destination_set_by`).

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
TEXT_COLUMNS = ("full_output", "summary", "handback_error", "delivery_error")


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.invocation_status ADD VALUE IF NOT EXISTS 'handing_back'")
    for name in TEXT_COLUMNS:
        op.add_column("invocations", sa.Column(name, sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(
        "invocations",
        sa.Column(
            "delivery_chat_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.chats.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_invocations_delivery_chat_id", "invocations", ["delivery_chat_id"], schema=SCHEMA
    )
    op.add_column(
        "triggers",
        sa.Column(
            "destination_chat_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.chats.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "triggers", sa.Column("destination_set_by", sa.Integer(), nullable=True), schema=SCHEMA
    )


def downgrade() -> None:
    """Postgres can't drop an enum value, so `handing_back` stays, unused: invocations still
    handing back are failed."""
    op.execute(
        f"UPDATE {SCHEMA}.invocations SET status = 'failed', finished_at = now(),"
        " error = 'The service stopped while summarising the result.'"
        " WHERE status = 'handing_back'"
    )
    op.drop_column("triggers", "destination_set_by", schema=SCHEMA)
    op.drop_column("triggers", "destination_chat_id", schema=SCHEMA)
    op.drop_index("ix_invocations_delivery_chat_id", "invocations", schema=SCHEMA)
    op.drop_column("invocations", "delivery_chat_id", schema=SCHEMA)
    for name in TEXT_COLUMNS:
        op.drop_column("invocations", name, schema=SCHEMA)
