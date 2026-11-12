"""archived chats

Chats have no owners, and admins archive and unarchive them: `aios.chats` gains when it was
archived and by which member (`archived_at`, `archived_by`), both null while it isn't. An
archived chat is read-only, and still read and searched.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When an admin archived it (read-only, still searchable); null while it isn't.",
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "chats",
        sa.Column(
            "archived_by",
            sa.Integer(),
            nullable=True,
            comment="The member (a platform user id) who archived it; null while it isn't.",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("chats", "archived_by", schema=SCHEMA)
    op.drop_column("chats", "archived_at", schema=SCHEMA)
