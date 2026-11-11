"""many memberships

A person can be in any number of organisations: the one-membership-per-user unique index goes,
and a plain index on the user takes its place, for listing a person's organisations.

The downgrade keeps only each user's most recently joined membership, so the unique index can
come back.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-11 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_organisation_members_user_id", table_name="organisation_members")
    op.create_index(
        "ix_organisation_members_user_id", "organisation_members", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM organisation_members m USING ("
            " SELECT organisation_id, user_id, row_number() OVER ("
            "  PARTITION BY user_id ORDER BY joined_at DESC, organisation_id DESC) AS n"
            " FROM organisation_members) ranked"
            " WHERE ranked.n > 1 AND m.organisation_id = ranked.organisation_id"
            " AND m.user_id = ranked.user_id"
        )
    )
    op.drop_index("ix_organisation_members_user_id", table_name="organisation_members")
    op.create_index(
        "uq_organisation_members_user_id", "organisation_members", ["user_id"], unique=True
    )
