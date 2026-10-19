"""chat names unique per organisation

Chats belong to the organisation (`aios.organisations`), so a chat's name is unique within it
rather than among every chat of the deployment: two organisations can each have "general". The
global unique constraint gives way to the organisation's index, as the agents', skills' and
workflows' did (0031).

Chats made since that revision and before saves stamped them are stamped first, as the data
step stamps them (its `backfill` is re-run: it only touches what has no organisation yet), so
none goes missing from the organisation's list and the index sees every row. Duplicates within
an organisation can't exist yet: until now names were unique among all chats.

The downgrade puts the global constraint back, which fails if two organisations have since
made chats of the same name.

Revision ID: 0043
Revises: 0031
Create Date: 2026-10-19 15:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0043"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill(conn) -> None:
    path = Path(__file__).with_name("0030_personal_organisations.py")
    spec = importlib.util.spec_from_file_location("personal_organisations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.backfill(conn)


def upgrade() -> None:
    _backfill(op.get_bind())
    op.drop_constraint("chats_name_key", "chats", type_="unique")
    op.create_index(
        "uq_chats_organisation_id_name", "chats", ["organisation_id", "name"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_chats_organisation_id_name", table_name="chats")
    op.create_unique_constraint("chats_name_key", "chats", ["name"])
