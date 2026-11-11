"""drop run history

The run history export is gone: past runs are searched through the search index
(`aios.search_items`, store revision 0015), so the export's copy of them goes, with its schema.

The `history` line's revisions stay, so a store at any of them is brought here as it's
migrated (`aios.datastores.provision.upgrade_store`); a store that never had the line never gets
it.

Revision ID: h0005
Revises: h0004
Create Date: 2026-11-13 12:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "h0005"
down_revision: str | None = "h0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "history"
# The line's revisions before this one, oldest first: going back makes their (empty) tables again.
EARLIER = (
    "h0001_history",
    "h0002_workflow_run_history",
    "h0003_history_search",
    "h0004_history_started_by",
)


def upgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def downgrade() -> None:
    here = Path(__file__).parent
    for stem in EARLIER:
        spec = importlib.util.spec_from_file_location(stem, here / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
