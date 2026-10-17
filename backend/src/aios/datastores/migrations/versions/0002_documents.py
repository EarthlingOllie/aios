"""documents and chunks

The store's uploaded documents and their chunks (text, embeddings, full-text index), moved
here from the platform database. They're as they were there, less `owner_id`: the store is
the owner.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "processing", "ready", "failed", name="document_status", schema=SCHEMA
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("contextualized", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # The type is the store's pgvector, in its `extensions` schema (on its search path).
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', contextualized)", persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], [f"{SCHEMA}.documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"], schema=SCHEMA)
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        schema=SCHEMA,
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin", schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("chunks", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.document_status")
