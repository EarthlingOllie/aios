"""run service work queue

`work_items`: the run service's queue (`aios.run_service`), and each organisation's in-flight
cap override (`organisations.in_flight_cap`, null for the default).

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organisations", sa.Column("in_flight_cap", sa.Integer(), nullable=True))
    op.create_table(
        "work_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lane", sa.Enum("interactive", "background", name="work_lane"), nullable=False),
        sa.Column(
            "kind", sa.Enum("invocation_start", "agent_turn", name="work_kind"), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ready_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=True),
        sa.UniqueConstraint("dedup_key", name="work_items_dedup_key_key"),
    )
    op.create_index("ix_work_items_organisation_id", "work_items", ["organisation_id"])
    op.create_index("ix_work_items_data_store_id", "work_items", ["data_store_id"])
    op.create_index("ix_work_items_ready_at", "work_items", ["ready_at"])


def downgrade() -> None:
    op.drop_table("work_items")
    op.execute("DROP TYPE work_kind")
    op.execute("DROP TYPE work_lane")
    op.drop_column("organisations", "in_flight_cap")
