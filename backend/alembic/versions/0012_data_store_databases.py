"""data store databases

Each data store gets a database of its own (`aios.datastores.provision`). Its row records
where that database is, its name and its roles' (derived from its id, never a user's), its
login roles' encrypted passwords, its status and its schema revision.

Existing stores start out `provisioning`, and the deploy's `migrate_all` provisions them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15 23:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = sa.Enum(
    "provisioning", "ready", "failed", "deleting", "deleted", name="data_store_status"
)


def upgrade() -> None:
    STATUS.create(op.get_bind())
    op.add_column(
        "data_stores",
        sa.Column("status", STATUS, server_default="provisioning", nullable=False),
    )
    op.add_column("data_stores", sa.Column("host", sa.String(255), nullable=True))
    op.add_column("data_stores", sa.Column("port", sa.Integer(), nullable=True))
    op.add_column("data_stores", sa.Column("dbname", sa.String(63), nullable=True))
    op.add_column("data_stores", sa.Column("role_prefix", sa.String(56), nullable=True))
    op.add_column("data_stores", sa.Column("rw_password", sa.Text(), nullable=True))
    op.add_column("data_stores", sa.Column("ro_password", sa.Text(), nullable=True))
    op.add_column("data_stores", sa.Column("schema_revision", sa.String(64), nullable=True))
    op.add_column("data_stores", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "data_stores", sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE data_stores SET dbname = 'ds_' || replace(id::text, '-', ''), "
        "role_prefix = 'ds_' || replace(id::text, '-', '')"
    )
    op.alter_column("data_stores", "dbname", nullable=False)
    op.alter_column("data_stores", "role_prefix", nullable=False)
    op.create_unique_constraint("data_stores_dbname_key", "data_stores", ["dbname"])
    op.create_unique_constraint("data_stores_role_prefix_key", "data_stores", ["role_prefix"])


def downgrade() -> None:
    op.drop_constraint("data_stores_role_prefix_key", "data_stores")
    op.drop_constraint("data_stores_dbname_key", "data_stores")
    for column in (
        "delete_after",
        "error",
        "schema_revision",
        "ro_password",
        "rw_password",
        "role_prefix",
        "dbname",
        "port",
        "host",
        "status",
    ):
        op.drop_column("data_stores", column)
    STATUS.drop(op.get_bind())
