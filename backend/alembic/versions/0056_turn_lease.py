"""turn lease

A run's turns take turns through a lease on its row (`aios.turn_lease`) instead of a session
advisory lock, which a connection pooler in transaction mode can't carry: a token, and the time
the lease lasts until. Both null while no turn holds it.

The checkpointer's connections no longer set their search path at connection startup (a pooler
can't pass startup settings on): it becomes a default of the app's role on this database, with
the checkpointer's schema (`langgraph`) after `public`, so unqualified names the app creates and
uses still land in `public`, while the checkpointer's own (which `public` doesn't have) are found
in `langgraph`. The downgrade drops both.

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _alter_role(action: str) -> None:
    op.execute(
        "DO $body$ BEGIN EXECUTE 'ALTER ROLE CURRENT_USER IN DATABASE '"
        f" || quote_ident(current_database()) || ' {action}'; END $body$"
    )


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    _alter_role("""SET search_path = "$user", public, langgraph""")


def downgrade() -> None:
    _alter_role("RESET search_path")
    op.drop_column("agent_runs", "lease_until")
    op.drop_column("agent_runs", "lease_token")
