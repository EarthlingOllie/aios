"""skill version names

`skill_versions.name` was added to 0002 after databases had already run it, so they're at head
without the column. Add it where it's missing, taking each version's name from its skill: the
name it would have been saved with had the column existed. Databases created since have it
from 0002, and this does nothing.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-18 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("skill_versions")}
    if "name" in columns:
        return
    op.add_column("skill_versions", sa.Column("name", sa.String(length=255), nullable=True))
    op.execute(
        "UPDATE skill_versions SET name = skills.name FROM skills"
        " WHERE skills.id = skill_versions.skill_id"
    )
    op.alter_column("skill_versions", "name", nullable=False)


def downgrade() -> None:
    # The column belongs to 0002 on databases created since; leave it.
    pass
