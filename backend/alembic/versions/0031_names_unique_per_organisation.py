"""names unique per organisation

Agents, skills and workflows belong to the organisation (`aios.organisations`), so their live
names are unique within it rather than per owner, and an organisation has one live agent of
each built-in kind (the documents assistant): the owner's unique indexes give way to the
organisation's. Kept per owner, a member who left and made "Reviewer" in their new
organisation would clash with the one they made in the old.

Rows made since the data step (0030) and before saves stamped them are stamped first, as it
stamps them (its `backfill` is re-run: it only touches what has no organisation yet), so none
goes missing from the organisation's lists and the indexes see every row. Duplicates within an
organisation can't exist yet: until now each organisation's rows were one owner's.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-19 12:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE = "deleted_at IS NULL"
BUILTIN_LIVE = "builtin IS NOT NULL AND deleted_at IS NULL"
# (table, column the name is unique with, where).
INDEXES = [
    ("skills", "name", LIVE),
    ("agents", "name", LIVE),
    ("workflows", "name", LIVE),
    ("agents", "builtin", BUILTIN_LIVE),
]


def _backfill(conn) -> None:
    path = Path(__file__).with_name("0030_personal_organisations.py")
    spec = importlib.util.spec_from_file_location("personal_organisations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.backfill(conn)


def upgrade() -> None:
    _backfill(op.get_bind())
    for table, column, where in INDEXES:
        op.drop_index(f"uq_{table}_owner_id_{column}_live", table_name=table)
        op.create_index(
            f"uq_{table}_organisation_id_{column}_live",
            table,
            ["organisation_id", column],
            unique=True,
            postgresql_where=sa.text(where),
        )


def downgrade() -> None:
    for table, column, where in INDEXES:
        op.drop_index(f"uq_{table}_organisation_id_{column}_live", table_name=table)
        op.create_index(
            f"uq_{table}_owner_id_{column}_live",
            table,
            ["owner_id", column],
            unique=True,
            postgresql_where=sa.text(where),
        )
