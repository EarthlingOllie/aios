"""roles

Every member has a role in each organisation they're in: admin, editor or participant
(`aios.permissions`). Each organisation's creator becomes its admin and everyone else an editor,
so nobody loses what they could do. An organisation whose creator has gone (deleted their
account, or left) has its earliest remaining member made admin, since an organisation always
needs one. Invitation links carry the role whoever joins through them is given, editor by
default.

The downgrade drops the roles: the creator-only checks they replaced aren't brought back.

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-11 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = postgresql.ENUM("admin", "editor", "participant", name="organisation_role")


def upgrade() -> None:
    ROLE.create(op.get_bind())
    for table in ("organisation_members", "organisation_invites"):
        op.add_column(
            table,
            sa.Column(
                "role",
                postgresql.ENUM(name="organisation_role", create_type=False),
                server_default="editor",
                nullable=False,
            ),
        )
    op.execute(
        """
        UPDATE organisation_members m SET role = 'admin'
        FROM organisations o
        WHERE o.id = m.organisation_id AND o.created_by = m.user_id
        """
    )
    op.execute(
        """
        UPDATE organisation_members m SET role = 'admin'
        FROM (
            SELECT DISTINCT ON (organisation_id) organisation_id, user_id
            FROM organisation_members
            ORDER BY organisation_id, joined_at, user_id
        ) first
        WHERE m.organisation_id = first.organisation_id AND m.user_id = first.user_id
          AND NOT EXISTS (
              SELECT 1 FROM organisation_members a
              WHERE a.organisation_id = m.organisation_id AND a.role = 'admin'
          )
        """
    )


def downgrade() -> None:
    op.drop_column("organisation_invites", "role")
    op.drop_column("organisation_members", "role")
    ROLE.drop(op.get_bind())
