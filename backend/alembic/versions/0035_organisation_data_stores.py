"""organisation data stores

The data store is the organisation's (`aios.datastores.resolve`): `owner_organisation_id`
(unique: one store per organisation) and the run history export's two flags, which were each
user's, move to `data_stores`. The data step points each store at its owner's organisation
(their personal one, from revision 0030) and copies their flags.

A store loses its organisation only when the organisation is deleted (`SET NULL`), which first
puts the store into `deleting` for the provisioner to drop: the check holds every other store
to an organisation. `owner_user_id` stays until the contract step drops it, so the release
before this one keeps finding its stores for the minutes of the rollout; nothing writes it
any more, so it's nullable now, and a user's deletion no longer takes a store with it.

`backfill` is the data step itself, over a connection, so it can be run (and tested) on its
own; it only touches stores with no organisation yet, so running it again changes nothing.

Revision ID: 0035
Revises: 0043
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0035"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAGS = ["export_run_history", "embed_run_history"]
CHECK = "ck_data_stores_owner_organisation"


def backfill(conn: Connection) -> None:
    conn.execute(
        sa.text(
            "UPDATE data_stores s SET owner_organisation_id = m.organisation_id,"
            " export_run_history = u.export_run_history, embed_run_history = u.embed_run_history"
            " FROM users u JOIN organisation_members m ON m.user_id = u.id"
            " WHERE u.id = s.owner_user_id AND s.owner_organisation_id IS NULL"
        )
    )


def upgrade() -> None:
    op.add_column("data_stores", sa.Column("owner_organisation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "data_stores_owner_organisation_id_fkey",
        "data_stores",
        "organisations",
        ["owner_organisation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "data_stores_owner_organisation_id_key", "data_stores", ["owner_organisation_id"]
    )
    for flag in FLAGS:
        op.add_column(
            "data_stores",
            sa.Column(flag, sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )
    op.alter_column("data_stores", "owner_user_id", nullable=True)
    op.drop_constraint("data_stores_owner_user_id_fkey", "data_stores", type_="foreignkey")
    op.create_foreign_key(
        "data_stores_owner_user_id_fkey",
        "data_stores",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    backfill(op.get_bind())
    op.create_check_constraint(
        CHECK,
        "data_stores",
        "owner_organisation_id IS NOT NULL OR status IN ('deleting', 'deleted')",
    )


def downgrade() -> None:
    # Back to a store per user: a store made since (with no owner) goes to its organisation's
    # first member who has none; any left over (and those of deleted organisations) loses its
    # row, as the previous release has no way to reach it (its database and roles are left for
    # an operator to drop: `provision.drop`).
    op.drop_constraint(CHECK, "data_stores", type_="check")
    op.execute(
        "UPDATE data_stores s SET owner_user_id = ("
        " SELECT m.user_id FROM organisation_members m"
        " WHERE m.organisation_id = s.owner_organisation_id"
        " AND NOT EXISTS (SELECT 1 FROM data_stores o WHERE o.owner_user_id = m.user_id)"
        " ORDER BY m.joined_at, m.user_id LIMIT 1)"
        " WHERE s.owner_user_id IS NULL"
    )
    op.execute("DELETE FROM data_stores WHERE owner_user_id IS NULL")
    op.execute(
        "UPDATE users u SET export_run_history = s.export_run_history,"
        " embed_run_history = s.embed_run_history"
        " FROM data_stores s WHERE s.owner_user_id = u.id"
    )
    op.drop_constraint("data_stores_owner_user_id_fkey", "data_stores", type_="foreignkey")
    op.create_foreign_key(
        "data_stores_owner_user_id_fkey",
        "data_stores",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("data_stores", "owner_user_id", nullable=False)
    for flag in FLAGS:
        op.drop_column("data_stores", flag)
    op.drop_constraint("data_stores_owner_organisation_id_key", "data_stores", type_="unique")
    op.drop_constraint("data_stores_owner_organisation_id_fkey", "data_stores", type_="foreignkey")
    op.drop_column("data_stores", "owner_organisation_id")
