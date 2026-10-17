"""history search

Searching past runs (`aios.search`): each exported message gets its text's embedding (filled in
by the export while its owner has embedding on, `aios.datastores.history`) and its full-text
vector (generated), with an index for each, and one on the messages still waiting to be
embedded.

Revision ID: h0003
Revises: h0002
Create Date: 2026-10-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "h0003"
down_revision: str | None = "h0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "history"
# As `aios.datastores.models.EMBEDDABLE` is now: a revision keeps its own copy.
EMBEDDABLE = "role IN ('user', 'assistant') AND btrim(text) <> ''"


def upgrade() -> None:
    op.add_column(
        "messages",
        # The type is the store's pgvector, in its `extensions` schema (on its search path).
        sa.Column(
            "embedding",
            Vector(1024),
            nullable=True,
            comment="Its text's embedding, for search: a user's or assistant's message with "
            "text, once embedded (only while embedding is on).",
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "messages",
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
            comment="Its text's full-text search vector.",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_messages_embedding_hnsw",
        "messages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        schema=SCHEMA,
    )
    op.create_index("ix_messages_tsv", "messages", ["tsv"], postgresql_using="gin", schema=SCHEMA)
    op.create_index(
        "ix_messages_unembedded",
        "messages",
        ["agent_run_id", "position"],
        postgresql_where=sa.text(f"embedding IS NULL AND {EMBEDDABLE}"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_unembedded", table_name="messages", schema=SCHEMA)
    op.drop_index("ix_messages_tsv", table_name="messages", schema=SCHEMA)
    op.drop_index("ix_messages_embedding_hnsw", table_name="messages", schema=SCHEMA)
    op.drop_column("messages", "tsv", schema=SCHEMA)
    op.drop_column("messages", "embedding", schema=SCHEMA)
