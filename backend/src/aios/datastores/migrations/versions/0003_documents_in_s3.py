"""documents' files in S3

Uploaded files move to S3 (`aios.datastores.files`): a document keeps where its file is, its
size and its SHA-256, and no longer the bytes themselves.

No store has a document yet (stores hold documents from the release this ships in), so there are
no bytes to move. Should one have, adding the NOT NULL columns fails, which stops that store's
migration, and reports it, rather than drop its files.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("s3_key", sa.String(length=1024), nullable=False), schema=SCHEMA
    )
    op.add_column(
        "documents", sa.Column("size_bytes", sa.BigInteger(), nullable=False), schema=SCHEMA
    )
    op.add_column(
        "documents", sa.Column("sha256", sa.String(length=64), nullable=False), schema=SCHEMA
    )
    op.drop_column("documents", "data", schema=SCHEMA)


def downgrade() -> None:
    # The bytes stay in S3: documents downgraded come back empty.
    op.add_column(
        "documents",
        sa.Column("data", sa.LargeBinary(), server_default=sa.text("''::bytea"), nullable=False),
        schema=SCHEMA,
    )
    op.alter_column("documents", "data", server_default=None, schema=SCHEMA)
    op.drop_column("documents", "sha256", schema=SCHEMA)
    op.drop_column("documents", "size_bytes", schema=SCHEMA)
    op.drop_column("documents", "s3_key", schema=SCHEMA)
