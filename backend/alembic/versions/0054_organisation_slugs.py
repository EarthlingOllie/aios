"""organisation slugs

Every organisation gets a unique slug, what a link in the browser names it by: made from its
name as `aios.organisations.slugify` makes it (lower-case ASCII words joined by `-`, or
`organisation` when nothing's left), suffixed `-2`, `-3`, ... on a clash, oldest organisation
first. A rename never changes it. The downgrade drops it.

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-11 10:00:00
"""

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# As `aios.organisations` has them at this revision; copied so this migration doesn't change
# when they do.
SLUG_MAX = 60
FALLBACK = "organisation"


def slugify(name: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return words[:SLUG_MAX].rstrip("-") or FALLBACK


def backfill(conn: Connection) -> None:
    taken: set[str] = set()
    rows = conn.execute(sa.text("SELECT id, name FROM organisations ORDER BY id")).all()
    for organisation_id, name in rows:
        base = slugify(name)
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        taken.add(slug)
        conn.execute(
            sa.text("UPDATE organisations SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": organisation_id},
        )


def upgrade() -> None:
    op.add_column("organisations", sa.Column("slug", sa.String(length=255), nullable=True))
    backfill(op.get_bind())
    op.alter_column("organisations", "slug", nullable=False)
    op.create_unique_constraint("organisations_slug_key", "organisations", ["slug"])


def downgrade() -> None:
    op.drop_constraint("organisations_slug_key", "organisations", type_="unique")
    op.drop_column("organisations", "slug")
