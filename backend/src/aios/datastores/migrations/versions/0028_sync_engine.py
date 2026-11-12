"""sync engine: synced items and source syncs

`aios.synced_items`: copies of what a source holds (`aios.sync`), one per external id per
source, with Jev's verdict. A message item keeps its text; a file item is a document
(`documents.synced_item_id`, unique: one document per file item).

`aios.source_syncs`: each source's syncs, with their counts and error; a partial unique index
keeps one running per source.

`aios.sources` gains `next_sync_at`: when it's next due.

`aios.search_items` gains the kind `source_item`: a message item's text, split, each piece by
its position (`synced_item_id`, deleted with it).

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-21 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
OLD_SOURCE = (
    "(kind = 'document_chunk') = (chunk_id IS NOT NULL)"
    " AND (kind = 'chat_message') = (message_id IS NOT NULL)"
    " AND (kind = 'run_message') = (turn_id IS NOT NULL AND position IS NOT NULL)"
    " AND (kind = 'brief') = (invocation_id IS NOT NULL)"
    " AND (kind::text = 'memory') = (memory_id IS NOT NULL)"
)
# The new enum value can't be used in the transaction that adds it, so the check compares text.
NEW_SOURCE = (
    OLD_SOURCE
    + " AND (kind::text = 'source_item') = (synced_item_id IS NOT NULL AND position IS NOT NULL)"
)
VERDICT = sa.Enum(name="verdict", schema=SCHEMA, create_type=False)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "next_sync_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it's next due to sync: its interval after the last, or a slow retry"
            " while failing. Null for Uploads, and for a source not synced yet (due at once).",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "synced_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"),
            nullable=False,
            comment="The source it was synced from.",
        ),
        sa.Column("external_id", sa.Text(), nullable=False, comment="Its id at its source."),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.synced_items.id", ondelete="SET NULL"),
            nullable=True,
            comment="The item it belongs to at its source (a reply's thread), if any.",
        ),
        sa.Column(
            "kind",
            sa.Enum("message", "file", name="synced_item_kind", schema=SCHEMA),
            nullable=False,
            comment="message or file.",
        ),
        sa.Column("title", sa.Text(), nullable=False, comment="Its title, or a name for it."),
        sa.Column("url", sa.Text(), nullable=True, comment="Where it is at its source."),
        sa.Column(
            "author_name",
            sa.Text(),
            nullable=True,
            comment="Who wrote it, as its source names them.",
        ),
        sa.Column(
            "source_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it last changed at its source.",
        ),
        sa.Column(
            "content_hash",
            sa.Text(),
            nullable=False,
            comment="The SHA-256 (hex) of its content: unchanged content isn't indexed again.",
        ),
        sa.Column("text", sa.Text(), nullable=True, comment="A message item's text."),
        sa.Column(
            "truncated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="Whether its text was cut at the cap.",
        ),
        sa.Column(
            "verdict",
            VERDICT,
            server_default="awaiting_check",
            nullable=False,
            comment="Jev's verdict: awaiting_check, clean, flagged or released.",
        ),
        sa.Column("verdict_reason", sa.Text(), nullable=True, comment="Why Jev flagged it."),
        sa.Column(
            "verdict_version",
            sa.Text(),
            nullable=True,
            comment="The version of Jev (model and policy) that judged it.",
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When Jev last judged it.",
        ),
        sa.Column(
            "released_by",
            sa.Integer(),
            nullable=True,
            comment="The admin who released it (aios.people.id).",
        ),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it was released.",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=_now(),
            nullable=False,
            comment="When a sync last found it at its source.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint(
            "source_id", "external_id", name="uq_synced_items_source_id_external_id"
        ),
        schema=SCHEMA,
        comment="Copies of what sources hold, kept in step with them by their syncs",
    )
    op.create_index("ix_synced_items_verdict", "synced_items", ["verdict"], schema=SCHEMA)
    op.create_table(
        "source_syncs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum("incremental", "full", "manual", name="sync_kind", schema=SCHEMA),
            nullable=False,
            comment="incremental, full or manual.",
        ),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", name="sync_status", schema=SCHEMA),
            server_default="running",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("removed", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "skipped",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="New items left out: the store was full.",
        ),
        sa.Column(
            "dropped",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="Items left out: too big, or unreadable.",
        ),
        sa.Column(
            "awaiting_check",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="Its source's items still waiting for Jev after it.",
        ),
        sa.Column(
            "storage_full",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="Whether it met the store's storage cap.",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        schema=SCHEMA,
        comment="Each source's syncs, with what each added, updated, removed and skipped",
    )
    op.create_index(
        "ix_source_syncs_source_id_started_at",
        "source_syncs",
        ["source_id", "started_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "uq_source_syncs_running",
        "source_syncs",
        ["source_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.add_column(
        "documents",
        sa.Column(
            "synced_item_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.synced_items.id"),
            nullable=True,
            comment="A synced file's synced item; null for an upload.",
        ),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_documents_synced_item_id", "documents", ["synced_item_id"], schema=SCHEMA
    )

    op.execute(f"ALTER TYPE {SCHEMA}.search_item_kind ADD VALUE IF NOT EXISTS 'source_item'")
    op.add_column(
        "search_items",
        sa.Column(
            "synced_item_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.synced_items.id", ondelete="CASCADE"),
            nullable=True,
            comment="A source item's synced item (its place in it is its position).",
        ),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_search_items_synced_item_id_position",
        "search_items",
        ["synced_item_id", "position"],
        schema=SCHEMA,
    )
    op.drop_constraint("ck_search_items_source", "search_items", schema=SCHEMA)
    op.create_check_constraint("ck_search_items_source", "search_items", NEW_SOURCE, schema=SCHEMA)


def downgrade() -> None:
    # A value can't be dropped from an enum: source items go, and `source_item` stays unused.
    op.execute(f"DELETE FROM {SCHEMA}.search_items WHERE kind::text = 'source_item'")
    op.drop_constraint("ck_search_items_source", "search_items", schema=SCHEMA)
    op.create_check_constraint("ck_search_items_source", "search_items", OLD_SOURCE, schema=SCHEMA)
    op.drop_constraint("uq_search_items_synced_item_id_position", "search_items", schema=SCHEMA)
    op.drop_column("search_items", "synced_item_id", schema=SCHEMA)
    # Synced documents go with their items (their files are left in S3).
    op.execute(f"DELETE FROM {SCHEMA}.documents WHERE synced_item_id IS NOT NULL")
    op.drop_constraint("uq_documents_synced_item_id", "documents", schema=SCHEMA)
    op.drop_column("documents", "synced_item_id", schema=SCHEMA)
    op.drop_table("source_syncs", schema=SCHEMA)
    op.drop_table("synced_items", schema=SCHEMA)
    op.drop_column("sources", "next_sync_at", schema=SCHEMA)
    for name in ("sync_status", "sync_kind", "synced_item_kind"):
        op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.{name}")
