"""budgets: the organisation's monthly budget and allowances

Organisations gain a monthly budget in estimated pounds sterling (null: none, the default) and
default monthly allowances by role; members gain their own allowance, an admin's override of
their role's (`aios.budgets`).

Revision ID: 0081
Revises: 0080
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organisations", sa.Column("budget", sa.Double(), nullable=True))
    op.add_column(
        "organisations",
        sa.Column(
            "role_allowances",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint("ck_organisations_budget", "organisations", "budget >= 0")
    op.add_column("organisation_members", sa.Column("allowance", sa.Double(), nullable=True))
    op.create_check_constraint(
        "ck_organisation_members_allowance", "organisation_members", "allowance >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_organisation_members_allowance", "organisation_members")
    op.drop_column("organisation_members", "allowance")
    op.drop_constraint("ck_organisations_budget", "organisations")
    op.drop_column("organisations", "role_allowances")
    op.drop_column("organisations", "budget")
