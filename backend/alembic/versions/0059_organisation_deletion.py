"""organisation deletion

An organisation's creator can schedule its deletion, with a grace period
(`aios.organisations.schedule_deletion`): when and by whom it was scheduled, and when the grace
period ends, on `organisations`, with a partial index for the sweep. Each organisation the sweep
deletes is recorded in `organisation_deletions`.

It also finishes the many-organisations work: names are no longer unique (slugs still are), and
the personal flag goes.

The downgrade drops the deletion columns and records, brings the flag back (false for every
organisation: none is personal now) and makes names unique again, suffixing each later
organisation's that repeats an earlier one's with its id.

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-11 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("organisations_name_key", "organisations", type_="unique")
    op.drop_column("organisations", "personal")
    op.add_column(
        "organisations",
        sa.Column("deletion_scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organisations",
        sa.Column(
            "deletion_scheduled_by",
            sa.Integer(),
            sa.ForeignKey(
                "users.id",
                ondelete="SET NULL",
                name="organisations_deletion_scheduled_by_fkey",
            ),
            nullable=True,
        ),
    )
    op.add_column("organisations", sa.Column("deletes_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_organisations_deletion_scheduled",
        "organisations",
        "(deletion_scheduled_at IS NULL) = (deletes_at IS NULL)",
    )
    op.create_index(
        "ix_organisations_deletes_at",
        "organisations",
        ["deletes_at"],
        postgresql_where=sa.text("deletes_at IS NOT NULL"),
    )
    op.create_table(
        "organisation_deletions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("scheduled_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_organisation_deletions_organisation_id",
        "organisation_deletions",
        ["organisation_id"],
    )


def downgrade() -> None:
    op.drop_table("organisation_deletions")
    op.drop_index("ix_organisations_deletes_at", table_name="organisations")
    op.drop_constraint("ck_organisations_deletion_scheduled", "organisations", type_="check")
    op.drop_column("organisations", "deletes_at")
    op.drop_column("organisations", "deletion_scheduled_by")
    op.drop_column("organisations", "deletion_scheduled_at")
    op.add_column(
        "organisations",
        sa.Column("personal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        sa.text(
            "UPDATE organisations o SET name = left(o.name, 240) || '-' || o.id"
            " WHERE EXISTS (SELECT 1 FROM organisations earlier"
            "  WHERE earlier.name = o.name AND earlier.id < o.id)"
        )
    )
    op.create_unique_constraint("organisations_name_key", "organisations", ["name"])
