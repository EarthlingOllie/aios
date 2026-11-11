"""drop the run history export

Past runs are searched through each store's search index now (store revision 0015), so the
run history export goes: its outbox (`history_exports`), and the export's and its embedding's
columns on the store registry. `history_revision` stays: a store that had the export's tables is
still on the store tree's `history` line, whose head drops them (store revision h0005).

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-13 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND = sa.Enum("agent_run", "workflow_run", name="history_export_kind")
TIMES = ("history_exported_at", "history_embedded_at", "history_embed_retry_at")
FLAGS = ("export_run_history", "embed_run_history")


def upgrade() -> None:
    op.drop_index("ix_history_exports_pending", table_name="history_exports")
    op.drop_table("history_exports")
    KIND.drop(op.get_bind())
    for column in (*TIMES, "history_embed_error", *FLAGS):
        op.drop_column("data_stores", column)


def downgrade() -> None:
    for column in FLAGS:
        op.add_column(
            "data_stores",
            sa.Column(column, sa.Boolean(), server_default=sa.text("false"), nullable=False),
        )
    for column in TIMES:
        op.add_column("data_stores", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.add_column("data_stores", sa.Column("history_embed_error", sa.Text(), nullable=True))
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
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
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
