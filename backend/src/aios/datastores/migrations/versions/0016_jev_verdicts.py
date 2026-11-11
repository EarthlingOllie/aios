"""Jev verdicts on documents and search items

`aios.verdict`: one set of verdicts everywhere (`aios.jev.Verdict`): awaiting_check, clean,
flagged, released and derived.

`aios.documents` gains its verdict (awaiting_check until Jev checks it), Jev's reason, the
version of Jev that judged it and when, the hash of the content it judged, and who released it
(and the content they released) if an admin did.

`aios.search_items` gains its verdict: a document chunk's copies its document's, everything else
is derived (what agents wrote, and people's chat messages until those are checked at posting).
Search finds only clean, released and derived items. Hash indexes on its text let `query_data`
find the stored verdict of text it reads from the platform's tables.

Documents already in the store wait for the awaiting-check sweep (`aios.verdicts.sweep`).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
VERDICTS = ("awaiting_check", "clean", "flagged", "released", "derived")


def upgrade() -> None:
    verdict = sa.Enum(*VERDICTS, name="verdict", schema=SCHEMA)
    verdict.create(op.get_bind())
    column = sa.Enum(*VERDICTS, name="verdict", schema=SCHEMA, create_type=False)
    op.add_column(
        "documents",
        sa.Column(
            "verdict",
            column,
            server_default="awaiting_check",
            nullable=False,
            comment="Jev's verdict: awaiting_check, clean, flagged or released.",
        ),
        schema=SCHEMA,
    )
    for name, type_, comment in (
        ("verdict_reason", sa.Text(), "Why Jev flagged it."),
        ("verdict_version", sa.Text(), "The version of Jev (model and policy) that judged it."),
        ("checked_at", sa.DateTime(timezone=True), "When Jev last judged it."),
        ("content_hash", sa.Text(), "The SHA-256 (hex) of the content Jev last judged."),
        ("released_by", sa.Integer(), "The admin who released it (aios.people.id)."),
        ("released_at", sa.DateTime(timezone=True), "When it was released."),
        ("released_content_hash", sa.Text(), "The SHA-256 (hex) of the content released."),
    ):
        op.add_column(
            "documents", sa.Column(name, type_, nullable=True, comment=comment), schema=SCHEMA
        )
    op.create_index(
        "ix_documents_verdict",
        "documents",
        ["verdict"],
        schema=SCHEMA,
    )
    op.add_column(
        "search_items",
        sa.Column(
            "verdict",
            column,
            server_default="awaiting_check",
            nullable=False,
            comment="Its source's Jev verdict: found by search only if clean, released or derived.",
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.search_items SET verdict = 'derived' WHERE kind <> 'document_chunk'"
    )
    op.create_index(
        "ix_search_items_text_md5",
        "search_items",
        [sa.text("md5(text)")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_search_items_search_text_md5",
        "search_items",
        [sa.text("md5(search_text)")],
        schema=SCHEMA,
        postgresql_where=sa.text("search_text IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_search_items_search_text_md5", "search_items", schema=SCHEMA)
    op.drop_index("ix_search_items_text_md5", "search_items", schema=SCHEMA)
    op.drop_column("search_items", "verdict", schema=SCHEMA)
    op.drop_index("ix_documents_verdict", "documents", schema=SCHEMA)
    for name in (
        "released_content_hash",
        "released_at",
        "released_by",
        "content_hash",
        "checked_at",
        "verdict_version",
        "verdict_reason",
        "verdict",
    ):
        op.drop_column("documents", name, schema=SCHEMA)
    sa.Enum(name="verdict", schema=SCHEMA).drop(op.get_bind())
