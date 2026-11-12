"""URL, webhook and email sources: a source's secret and token

Sources gain, for webhook and email sources, an encrypted secret (a webhook source's signing
secret) and the SHA-256 of what leads to them from outside (a webhook source's token, an email
source's address): unique, and both on the deny list.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column("sources", sa.Column("secret", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("sources", sa.Column("token_hash", sa.String(64), nullable=True), schema=SCHEMA)
    op.create_index("uq_sources_token_hash", "sources", ["token_hash"], unique=True, schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("uq_sources_token_hash", "sources", schema=SCHEMA)
    op.drop_column("sources", "token_hash", schema=SCHEMA)
    op.drop_column("sources", "secret", schema=SCHEMA)
