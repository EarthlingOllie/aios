"""memories

`aios.memories`: what each agent chose to remember across its runs (`aios.memory`), with where it
was learned (its run, turn and chat, cleared rather than the memory when they go) and who last
edited it. One memory per agent per normalised text.

`aios.search_items` gains the kind `memory`, a memory's item (`memory_id`, deleted with it): an
agent recalls its own memories through search.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
OLD_SOURCE = (
    "(kind = 'document_chunk') = (chunk_id IS NOT NULL)"
    " AND (kind = 'chat_message') = (message_id IS NOT NULL)"
    " AND (kind = 'run_message') = (turn_id IS NOT NULL AND position IS NOT NULL)"
    " AND (kind = 'brief') = (invocation_id IS NOT NULL)"
)
# The new enum value can't be used in the transaction that adds it, so the check compares text.
NEW_SOURCE = OLD_SOURCE + " AND (kind::text = 'memory') = (memory_id IS NOT NULL)"


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"),
            nullable=False,
            comment="The agent whose memory it is.",
        ),
        sa.Column("content", sa.Text(), nullable=False, comment="What it remembers."),
        sa.Column(
            "normalised",
            sa.Text(),
            nullable=False,
            comment="Its content in lower case with whitespace collapsed: the same fact once.",
        ),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", ondelete="SET NULL"),
            nullable=True,
            comment="The run that wrote it.",
        ),
        sa.Column(
            "turn_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.turns.id", ondelete="SET NULL"),
            nullable=True,
            comment="The turn that wrote it.",
        ),
        sa.Column(
            "chat_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.chats.id", ondelete="SET NULL"),
            nullable=True,
            comment="The chat it was learned in.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was first learned.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was last remembered again or edited.",
        ),
        sa.Column(
            "updated_by",
            sa.Integer(),
            nullable=True,
            comment="The person who last edited it (aios.people.id); null: the agent.",
        ),
        schema=SCHEMA,
        comment="Agents' memories: what each chose to remember across its runs",
    )
    op.create_index(
        "uq_memories_agent_id_normalised",
        "memories",
        ["agent_id", "normalised"],
        unique=True,
        schema=SCHEMA,
    )
    op.execute(f"ALTER TYPE {SCHEMA}.search_item_kind ADD VALUE IF NOT EXISTS 'memory'")
    op.alter_column(
        "search_items",
        "kind",
        existing_type=sa.Enum(name="search_item_kind", schema=SCHEMA),
        existing_nullable=False,
        comment="document_chunk, chat_message, run_message, brief or memory.",
        schema=SCHEMA,
    )
    op.add_column(
        "search_items",
        sa.Column(
            "memory_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{SCHEMA}.memories.id", ondelete="CASCADE"),
            nullable=True,
            comment="A memory's memory.",
        ),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_search_items_memory_id", "search_items", ["memory_id"], schema=SCHEMA
    )
    op.drop_constraint("ck_search_items_source", "search_items", schema=SCHEMA)
    op.create_check_constraint("ck_search_items_source", "search_items", NEW_SOURCE, schema=SCHEMA)


def downgrade() -> None:
    # A value can't be dropped from an enum: memories' items go, and `memory` stays unused.
    op.execute(f"DELETE FROM {SCHEMA}.search_items WHERE kind::text = 'memory'")
    op.drop_constraint("ck_search_items_source", "search_items", schema=SCHEMA)
    op.create_check_constraint("ck_search_items_source", "search_items", OLD_SOURCE, schema=SCHEMA)
    op.drop_constraint("uq_search_items_memory_id", "search_items", schema=SCHEMA)
    op.drop_column("search_items", "memory_id", schema=SCHEMA)
    op.alter_column(
        "search_items",
        "kind",
        existing_type=sa.Enum(name="search_item_kind", schema=SCHEMA),
        existing_nullable=False,
        comment="document_chunk, chat_message, run_message or brief.",
        schema=SCHEMA,
    )
    op.drop_table("memories", schema=SCHEMA)
