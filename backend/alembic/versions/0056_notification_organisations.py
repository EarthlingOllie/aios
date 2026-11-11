"""notification organisations

Each notification is from an organisation: `organisation_id`, so a person's notifications from
every organisation they're in can each say which, and open in it. Every existing one is about a
schema change request, whose store's organisation it gets; one whose store has no organisation
any more (it was deleted) is about nothing anyone can open, and goes. A notification goes with
its organisation. The downgrade drops the column.

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-11 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("organisation_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE notifications n
        SET organisation_id = s.owner_organisation_id
        FROM data_stores s
        WHERE s.id::text = n.ref->>'data_store_id'
        """
    )
    op.execute("DELETE FROM notifications WHERE organisation_id IS NULL")
    op.alter_column("notifications", "organisation_id", nullable=False)
    op.create_foreign_key(
        "fk_notifications_organisation_id_organisations",
        "notifications",
        "organisations",
        ["organisation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_notifications_organisation_id", "notifications", ["organisation_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_organisation_id", table_name="notifications")
    op.drop_constraint(
        "fk_notifications_organisation_id_organisations", "notifications", type_="foreignkey"
    )
    op.drop_column("notifications", "organisation_id")
