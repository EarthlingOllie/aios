"""search items

`aios.search_items`: the search index (`aios.search_index`), one item per document chunk, chat
message, user's or assistant's message of a run, and handoff brief, written in the same
transaction as its source and deleted with it. Embedded in the background, so its embedding is
nullable until then; its full-text vector is generated.

A chunk's own embedding is no longer written (its item's is): it becomes nullable.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-13 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
KINDS = ("document_chunk", "chat_message", "run_message", "brief")


def upgrade() -> None:
    op.create_table(
        "search_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "kind",
            sa.Enum(*KINDS, name="search_item_kind", schema=SCHEMA),
            nullable=False,
            comment="document_chunk, chat_message, run_message or brief.",
        ),
        sa.Column(
            "chunk_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.chunks.id", ondelete="CASCADE"),
            nullable=True,
            comment="A document chunk's chunk.",
        ),
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.chat_messages.id", ondelete="CASCADE"),
            nullable=True,
            comment="A chat message's message.",
        ),
        sa.Column(
            "turn_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.turns.id", ondelete="CASCADE"),
            nullable=True,
            comment="A run message's turn.",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=True,
            comment="A run message's place in its run's conversation, from 0.",
        ),
        sa.Column(
            "invocation_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.invocations.id", ondelete="CASCADE"),
            nullable=True,
            comment="A brief's invocation.",
        ),
        sa.Column(
            "ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "author_user_id",
            sa.Integer(),
            nullable=True,
            comment="The person who wrote it, if a person did.",
        ),
        sa.Column("text", sa.Text(), nullable=False, comment="Its text, as shown."),
        sa.Column(
            "search_text",
            sa.Text(),
            nullable=True,
            comment="What's searched and embedded instead of its text, if not its text.",
        ),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(search_text, text))", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "text_hash",
            sa.Text(),
            nullable=False,
            comment="The SHA-256 (hex) of what's searched: unchanged text isn't re-embedded.",
        ),
        # The type is the store's pgvector, in its `extensions` schema (on its search path).
        sa.Column(
            "embedding",
            Vector(1024),
            nullable=True,
            comment="What's searched's embedding; null until embedded.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'document_chunk') = (chunk_id IS NOT NULL)"
            " AND (kind = 'chat_message') = (message_id IS NOT NULL)"
            " AND (kind = 'run_message') = (turn_id IS NOT NULL AND position IS NOT NULL)"
            " AND (kind = 'brief') = (invocation_id IS NOT NULL)",
            name="ck_search_items_source",
        ),
        sa.UniqueConstraint("chunk_id", name="uq_search_items_chunk_id"),
        sa.UniqueConstraint("message_id", name="uq_search_items_message_id"),
        sa.UniqueConstraint("turn_id", "position", name="uq_search_items_turn_id_position"),
        sa.UniqueConstraint("invocation_id", name="uq_search_items_invocation_id"),
        schema=SCHEMA,
        comment="The search index: documents, chat messages, runs' messages and briefs",
    )
    op.create_index(
        "ix_search_items_embedding_hnsw",
        "search_items",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        schema=SCHEMA,
    )
    op.create_index(
        "ix_search_items_tsv", "search_items", ["tsv"], postgresql_using="gin", schema=SCHEMA
    )
    op.create_index(
        "ix_search_items_unembedded",
        "search_items",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_search_items_text_hash",
        "search_items",
        ["text_hash"],
        postgresql_where=sa.text("embedding IS NOT NULL"),
        schema=SCHEMA,
    )
    for column in ("created_at", "chat_id", "agent_id", "agent_run_id"):
        op.create_index(f"ix_search_items_{column}", "search_items", [column], schema=SCHEMA)
    op.alter_column("chunks", "embedding", existing_type=Vector(1024), nullable=True, schema=SCHEMA)


def downgrade() -> None:
    # Chunks ingested since have no embedding of their own: the release before needs one.
    op.execute(f"DELETE FROM {SCHEMA}.chunks WHERE embedding IS NULL")
    op.alter_column(
        "chunks", "embedding", existing_type=Vector(1024), nullable=False, schema=SCHEMA
    )
    op.drop_table("search_items", schema=SCHEMA)
    sa.Enum(name="search_item_kind", schema=SCHEMA).drop(op.get_bind())
