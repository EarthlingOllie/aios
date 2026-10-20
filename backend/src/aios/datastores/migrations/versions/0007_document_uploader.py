"""document uploader

Who uploaded each document: a member of the store's organisation, by their platform user id.
Not a foreign key, since a store has no users table (and a user can be deleted while their
documents stay); documents from before stay null.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("uploaded_by", sa.Integer(), nullable=True), schema="aios")


def downgrade() -> None:
    op.drop_column("documents", "uploaded_by", schema="aios")
