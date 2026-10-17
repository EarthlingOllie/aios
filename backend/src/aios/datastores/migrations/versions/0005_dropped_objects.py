"""dropped objects

The tables and columns in `data` that approved drops renamed out of the way
(`_dropped_<timestamp>_<name>`), until they're restored or purged. Only the owner (schema
changes, restores and the purge) writes it (`provision.BOOKKEEPING`); both login roles read it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dropped_objects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        # A dropped table: its name before it was dropped. A dropped column: the table it's in,
        # as that's named now (a dropped table's new name, if the table was dropped after it).
        sa.Column("table_name", sa.Text(), nullable=False),
        # A dropped column: its name before it was dropped. None for a table.
        sa.Column("column_name", sa.Text(), nullable=True),
        # What it was renamed to.
        sa.Column("dropped_name", sa.Text(), nullable=False),
        # A column that was NOT NULL (dropping it lifts that, so new rows can leave it out).
        sa.Column("not_null", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "dropped_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Why the last restore the owner asked for was refused, if it was.
        sa.Column("restore_error", sa.Text(), nullable=True),
        schema="aios",
        comment="Tables and columns in data that were dropped, until restored or purged",
    )


def downgrade() -> None:
    op.drop_table("dropped_objects", schema="aios")
