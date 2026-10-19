"""organisations

Users belong to organisations, which own what their members make (`aios.organisations`): the
expand step. The three tables (organisations, their members and their invitation links), and
the provenance columns on everything a user owns today: `organisation_id` next to `owner_id`
(nullable, until the data step has filled it and the contract step drops `owner_id`),
`created_by` and `updated_by`, `deleted_by` where there's a `deleted_at`, and `created_by` on
runs (null for a schedule's).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-19 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# What a user owns today, and whether it's soft-deleted (and so records who deleted it).
OWNED = {"skills": True, "agents": True, "workflows": True, "triggers": False, "chats": False}
RUNS = ["agent_runs", "workflow_runs"]


def _user_column(table: str, column: str) -> None:
    """A nullable reference to the member who did something, kept (null) once they're deleted."""
    op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
    op.create_foreign_key(
        f"{table}_{column}_fkey", table, "users", [column], ["id"], ondelete="SET NULL"
    )


def _drop_user_column(table: str, column: str) -> None:
    op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
    op.drop_column(table, column)


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("personal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "organisation_invites",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_organisation_invites_organisation_id"),
        "organisation_invites",
        ["organisation_id"],
    )
    op.create_table(
        "organisation_members",
        sa.Column("organisation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("invite_id", sa.UUID(), nullable=True),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invite_id"], ["organisation_invites.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organisation_id", "user_id"),
    )
    # One organisation per user, for now.
    op.create_index(
        "uq_organisation_members_user_id", "organisation_members", ["user_id"], unique=True
    )

    for table, soft_deleted in OWNED.items():
        op.add_column(table, sa.Column("organisation_id", sa.Integer(), nullable=True))
        op.create_index(op.f(f"ix_{table}_organisation_id"), table, ["organisation_id"])
        op.create_foreign_key(
            f"{table}_organisation_id_fkey",
            table,
            "organisations",
            ["organisation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        _user_column(table, "created_by")
        _user_column(table, "updated_by")
        if soft_deleted:
            _user_column(table, "deleted_by")
    for table in RUNS:
        _user_column(table, "created_by")


def downgrade() -> None:
    for table in RUNS:
        _drop_user_column(table, "created_by")
    for table, soft_deleted in OWNED.items():
        if soft_deleted:
            _drop_user_column(table, "deleted_by")
        _drop_user_column(table, "updated_by")
        _drop_user_column(table, "created_by")
        op.drop_constraint(f"{table}_organisation_id_fkey", table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_organisation_id"), table_name=table)
        op.drop_column(table, "organisation_id")
    op.drop_index("uq_organisation_members_user_id", table_name="organisation_members")
    op.drop_table("organisation_members")
    op.drop_index(
        op.f("ix_organisation_invites_organisation_id"), table_name="organisation_invites"
    )
    op.drop_table("organisation_invites")
    op.drop_table("organisations")
