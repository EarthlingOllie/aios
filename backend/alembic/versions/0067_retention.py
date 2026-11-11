"""retention settings

How many days an organisation keeps its finished runs, and its chats' messages
(`aios.retention`): null, the default, keeps them indefinitely.

Revision ID: 0067
Revises: 0066
Create Date: 2026-11-14 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("run_retention_days", "chat_retention_days")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("organisations", sa.Column(column, sa.Integer(), nullable=True))
        op.create_check_constraint(
            f"ck_organisations_{column}", "organisations", f"{column} BETWEEN 1 AND 36500"
        )


def downgrade() -> None:
    for column in COLUMNS:
        op.drop_constraint(f"ck_organisations_{column}", "organisations", type_="check")
        op.drop_column("organisations", column)
