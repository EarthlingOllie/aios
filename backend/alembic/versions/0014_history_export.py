"""history export

The run history export (`aios.datastores.history`): the per-user setting, its outbox
(`history_exports`), and on each data store the `history` branch's revision and when the export
last ran.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND = sa.Enum("agent_run", name="history_export_kind")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "export_run_history", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column("data_stores", sa.Column("history_revision", sa.String(64), nullable=True))
    op.add_column(
        "data_stores",
        sa.Column("history_exported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "history_exports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("data_store_id", sa.UUID(), nullable=False),
        sa.Column("kind", KIND, nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["data_store_id"], ["data_stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("data_store_id", "kind", "source_id"),
    )
    op.create_index(
        "ix_history_exports_pending",
        "history_exports",
        ["data_store_id", "enqueued_at"],
        postgresql_where=sa.text("exported_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_history_exports_pending", table_name="history_exports")
    op.drop_table("history_exports")
    KIND.drop(op.get_bind())
    op.drop_column("data_stores", "history_exported_at")
    op.drop_column("data_stores", "history_revision")
    op.drop_column("users", "export_run_history")
