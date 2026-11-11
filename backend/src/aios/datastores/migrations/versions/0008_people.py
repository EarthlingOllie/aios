"""people mirror

`aios.people`: one row per person who is or was a member of the store's organisation, with their
names, email and role, and when they joined and left. The platform keeps it up to date
(`aios.datastores.people`); a store migrated to here is filled straight after
(`provision.migrate_store`). Their email is kept from the read-only role by `provision`, which
knows the store's role names (`provision.DENIED_COLUMNS`).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "people",
        # Their platform user id.
        sa.Column("id", sa.BigInteger(), autoincrement=False, primary_key=True),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        # admin, editor or participant.
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        # Null while they're a member.
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema="aios",
        comment="The organisation's people, current and former members, with their roles",
    )


def downgrade() -> None:
    op.drop_table("people", schema="aios")
