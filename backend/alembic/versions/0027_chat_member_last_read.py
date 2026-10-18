"""chat member last read

Each user member of a chat keeps the id of the last of its messages they've seen, from which the
chat list counts what they haven't (null: they've seen none).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-18 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_members", sa.Column("last_read_message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_members", "last_read_message_id")
