"""sources, and Uploads

`aios.sources`: what brings content into the store. Every store gets exactly one upload source,
Uploads, made here (so with the store, when it's provisioned), and every document now belongs to
a source: documents already in the store are Uploads', and a document can't be written without
one. A partial unique index keeps Uploads the only upload source.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "kind",
            sa.Enum(
                "upload", "url", "webhook", "email", "connector", name="source_kind", schema=SCHEMA
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        # Never secrets.
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "paused",
                "failing",
                "needs_reauth",
                "removing",
                name="source_status",
                schema=SCHEMA,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        # A platform user id; null for Uploads.
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
        schema=SCHEMA,
        comment="What brings content into the store: Uploads, and later synced sources",
    )
    op.create_index(
        "uq_sources_upload",
        "sources",
        ["kind"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("kind = 'upload'"),
    )
    op.execute(f"INSERT INTO {SCHEMA}.sources (kind, name) VALUES ('upload', 'Uploads')")

    op.add_column("documents", sa.Column("source_id", sa.Integer(), nullable=True), schema=SCHEMA)
    op.execute(
        f"UPDATE {SCHEMA}.documents SET source_id ="
        f" (SELECT id FROM {SCHEMA}.sources WHERE kind = 'upload')"
    )
    op.alter_column("documents", "source_id", nullable=False, schema=SCHEMA)
    op.create_foreign_key(
        "documents_source_id_fkey",
        "documents",
        "sources",
        ["source_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_documents_source_id", "documents", schema=SCHEMA)
    op.drop_constraint("documents_source_id_fkey", "documents", schema=SCHEMA)
    op.drop_column("documents", "source_id", schema=SCHEMA)
    op.drop_table("sources", schema=SCHEMA)
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.source_status")
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.source_kind")
