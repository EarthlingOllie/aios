"""chat invocation handoff

Before a mention's run starts, the handoff step (`aios.handoff`) briefs it on the chat: the
invocation is `briefing` between `queued` and `running`, and keeps the brief its run was told.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-18 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_unfinished_index(statuses: str) -> None:
    # A chat's unfinished invocations: what its poll reports on.
    op.create_index(
        "ix_chat_invocations_unfinished",
        "chat_invocations",
        ["chat_id"],
        unique=False,
        postgresql_where=sa.text(f"status IN ({statuses})"),
    )


def upgrade() -> None:
    # Committed on its own: a new enum value can't be used in the transaction that adds it, and
    # the index below names it.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE chat_invocation_status ADD VALUE IF NOT EXISTS 'briefing' AFTER 'queued'"
        )
    op.add_column("chat_invocations", sa.Column("brief", sa.Text(), nullable=True))
    op.drop_index("ix_chat_invocations_unfinished", table_name="chat_invocations")
    _create_unfinished_index("'queued', 'briefing', 'running'")


def downgrade() -> None:
    op.drop_index("ix_chat_invocations_unfinished", table_name="chat_invocations")
    op.drop_column("chat_invocations", "brief")
    # Postgres can't drop an enum value: briefing invocations go back to waiting to start, and
    # the type is made again without it.
    op.execute("UPDATE chat_invocations SET status = 'queued' WHERE status = 'briefing'")
    op.execute("ALTER TYPE chat_invocation_status RENAME TO chat_invocation_status_old")
    op.execute(
        "CREATE TYPE chat_invocation_status AS ENUM ('queued', 'running', 'succeeded', 'failed')"
    )
    op.execute(
        "ALTER TABLE chat_invocations ALTER COLUMN status TYPE chat_invocation_status"
        " USING status::text::chat_invocation_status"
    )
    op.execute("DROP TYPE chat_invocation_status_old")
    _create_unfinished_index("'queued', 'running'")
