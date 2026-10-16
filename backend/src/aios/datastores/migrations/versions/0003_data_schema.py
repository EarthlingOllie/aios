"""data schema

The owner's tables: agents read them (`describe_data`, `query_data`), and only approved schema
change requests change them, so this tree creates the schema and nothing in it (`include_name`
leaves it out). Made as the owner, the store's default privileges give both login roles
`USAGE` on it and `SELECT` on its tables, and the read-write role `INSERT`, `UPDATE` and
`DELETE` (`aios.datastores.provision`).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 10:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS data")
    op.execute("COMMENT ON SCHEMA data IS 'Tables the owner''s agents keep their data in'")


def downgrade() -> None:
    # RESTRICT: never takes the owner's tables with it.
    op.execute("DROP SCHEMA IF EXISTS data RESTRICT")
