"""soft-delete agents and workflows

Workflow versions pin agent versions and other workflows' versions, so deleting an agent or a
workflow keeps its row (and its versions) and only marks it deleted, like skills. Names are
unique among live rows only, so a deleted one's name can be reused.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("agents", "workflows")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.drop_constraint(f"{table}_owner_id_name_key", table, type_="unique")
        op.create_index(
            f"uq_{table}_owner_id_name_live",
            table,
            ["owner_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    for table in TABLES:
        # Deleted rows can't be dropped (workflow versions may pin their versions), so they come
        # back as live rows, renamed so the old (owner_id, name) constraint holds.
        op.execute(
            f"UPDATE {table} SET name = left(name, 200) || ' (deleted #' || id || ')'"
            " WHERE deleted_at IS NOT NULL"
        )
        op.drop_index(
            f"uq_{table}_owner_id_name_live",
            table_name=table,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
        op.create_unique_constraint(f"{table}_owner_id_name_key", table, ["owner_id", "name"])
        op.drop_column(table, "deleted_at")
