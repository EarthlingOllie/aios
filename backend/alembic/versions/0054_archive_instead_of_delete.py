"""archive instead of delete

Agents, skills and workflows are archived rather than deleted: each gains when it was archived
and by whom (`archived_at`, `archived_by`), and loses its soft-delete columns. An archived item's
name stays reserved, so the organisation's name indexes cover every row, not only live ones, and
so does the one built-in agent of each kind.

aios isn't live, and dev data is rebuilt with `make seed`, so nothing is kept for the rows
deleted earlier beyond what a database needs to get here: they become archived (versions still
pin them, so they can't go), and one whose name a live row has since taken is renamed
"<name> (archived <id>)" so the indexes can be made.

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["skills", "agents", "workflows"]


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "archived_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.execute(
            f"UPDATE {table} SET archived_at = deleted_at, archived_by = deleted_by"
            " WHERE deleted_at IS NOT NULL"
        )
        # A deleted row whose name another row of the organisation has (a live one, or an
        # older deleted one) is renamed, so the names can be unique across every row.
        op.execute(
            f"""
            UPDATE {table} t SET name = left(t.name, 200) || ' (archived ' || t.id || ')'
             WHERE t.deleted_at IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM {table} o
                    WHERE o.organisation_id = t.organisation_id AND o.name = t.name
                      AND o.id <> t.id
                      AND (o.deleted_at IS NULL OR o.id > t.id)
               )
            """
        )
        op.drop_index(f"uq_{table}_organisation_id_name_live", table_name=table)
        op.create_index(
            f"uq_{table}_organisation_id_name", table, ["organisation_id", "name"], unique=True
        )
    # One built-in agent of each kind, archived or not: a deleted one goes back to being a
    # member's own agent, so the live one stays the organisation's.
    op.execute(
        """
        UPDATE agents a SET builtin = NULL
         WHERE a.deleted_at IS NOT NULL AND a.builtin IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM agents o
                WHERE o.organisation_id = a.organisation_id AND o.builtin = a.builtin
                  AND o.id <> a.id AND (o.deleted_at IS NULL OR o.id > a.id)
           )
        """
    )
    op.drop_index("uq_agents_organisation_id_builtin_live", table_name="agents")
    op.create_index(
        "uq_agents_organisation_id_builtin",
        "agents",
        ["organisation_id", "builtin"],
        unique=True,
        postgresql_where=sa.text("builtin IS NOT NULL"),
    )
    for table in TABLES:
        op.drop_column(table, "deleted_by")
        op.drop_column(table, "deleted_at")


def downgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "deleted_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.execute(f"UPDATE {table} SET deleted_at = archived_at, deleted_by = archived_by")
        op.drop_index(f"uq_{table}_organisation_id_name", table_name=table)
        op.create_index(
            f"uq_{table}_organisation_id_name_live",
            table,
            ["organisation_id", "name"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    op.drop_index("uq_agents_organisation_id_builtin", table_name="agents")
    op.create_index(
        "uq_agents_organisation_id_builtin_live",
        "agents",
        ["organisation_id", "builtin"],
        unique=True,
        postgresql_where=sa.text("builtin IS NOT NULL AND deleted_at IS NULL"),
    )
    for table in TABLES:
        op.drop_column(table, "archived_by")
        op.drop_column(table, "archived_at")
