"""budgets: running spend totals, and who each invocation is charged to

`aios.spend_totals`: the usage ledger's cost per calendar month in UTC, for the organisation
(person 0) and each person, kept in step with the ledger and reconciled nightly
(`aios.budgets`), from what the ledger holds already.

Invocations gain `charged_to`: their chain of causes' root person, whom budgets charge;
existing ones are charged to whoever asked for them.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "invocations", sa.Column("charged_to", sa.Integer(), nullable=True), schema=SCHEMA
    )
    op.execute(f"UPDATE {SCHEMA}.invocations SET charged_to = requested_by")
    op.create_table(
        "spend_totals",
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("person_id", sa.Integer(), primary_key=True),
        sa.Column("cost", sa.Double(), nullable=False, server_default=sa.text("0")),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema=SCHEMA,
    )
    month = "date_trunc('month', timezone('UTC', at))::date"
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.spend_totals (month, person_id, cost)
        SELECT {month}, 0, sum(cost) FROM {SCHEMA}.usage
        WHERE cost IS NOT NULL GROUP BY 1
        UNION ALL
        SELECT {month}, person_id, sum(cost) FROM {SCHEMA}.usage
        WHERE cost IS NOT NULL AND person_id IS NOT NULL GROUP BY 1, 2
        """
    )


def downgrade() -> None:
    op.drop_table("spend_totals", schema=SCHEMA)
    op.drop_column("invocations", "charged_to", schema=SCHEMA)
