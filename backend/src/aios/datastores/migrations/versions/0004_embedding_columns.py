"""embedding columns

Marks which `data` columns are embeddings of which text columns: `write_data` fills those with
the platform's embedding model, and schema changes add a row here as they add such a column.
Only the owner (schema changes) writes it; both login roles read it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding_columns",
        # A `vector(EMBEDDING_DIM)` column of a table in `data`, and the text column in the same
        # table it's the embedding of.
        sa.Column("table_name", sa.Text(), primary_key=True),
        sa.Column("column_name", sa.Text(), primary_key=True),
        sa.Column("source_column", sa.Text(), nullable=False),
        schema="aios",
        comment="Columns in data that write_data fills with embeddings of a text column",
    )
    # The store's default privileges let its read-write role write every table the owner makes,
    # but not this one. The tree runs as the owner, `<prefix>_owner`; the role is `<prefix>_rw`.
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
                'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON aios.embedding_columns FROM %I',
                regexp_replace(current_user, '_owner$', '_rw')
            );
        END $$
        """
    )


def downgrade() -> None:
    op.drop_table("embedding_columns", schema="aios")
