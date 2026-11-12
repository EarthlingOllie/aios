"""approval expiry setting

Organisations gain how many days an approval waits undecided before it expires
(`approval_expiry_days`, 1 to 365, default 7): schema changes' and workflow approval steps'
alike (`aios.approvals`). It replaces the deployment-wide schema change one, which is kept only
for approvals made before expiry times were recorded, until the rename to approvals drops it.

Revision ID: 0076
Revises: 0075
Create Date: 2026-11-21 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organisations",
        sa.Column(
            "approval_expiry_days", sa.Integer(), server_default=sa.text("7"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_organisations_approval_expiry_days",
        "organisations",
        "approval_expiry_days BETWEEN 1 AND 365",
    )


def downgrade() -> None:
    op.drop_constraint("ck_organisations_approval_expiry_days", "organisations")
    op.drop_column("organisations", "approval_expiry_days")
