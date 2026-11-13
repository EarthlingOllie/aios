"""approvals: the rename, dropping the old forms

`aios.schema_change_requests` is renamed `aios.approvals` (no view is left under the old name),
with its keys, indexes, constraints and status type (`approval_status`). The Insights views that
read it (`insights.approvals`, `insights.workflow_steps`) follow it, as views name a table by
its identity, not its name.

The old forms go with it:

- `workflow_run_steps.waiting_on_request_id`, which `approval_id` replaced (0027) and nothing
  writes any more;
- the deployment's schema change expiry (`SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`), which only
  approvals made before expiry times were recorded still expired by: each is given the expiry
  time that setting's default gave it (a week after it was made), and every approval has one
  from now on.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
OLD, NEW = "schema_change_requests", "approvals"

# The table's constraints and indexes, by their old names: renamed from `schema_change_requests`
# to `approvals` within each.
CONSTRAINTS = (
    "schema_change_requests_pkey",
    "uq_schema_change_requests_run_tool_call",
    "schema_change_requests_requested_by_run_fkey",
    "fk_schema_change_requests_workflow_step_id",
    "ck_schema_change_requests_kind",
    "ck_schema_change_requests_approver_role",
    "ck_schema_change_requests_schema_change",
    "ck_schema_change_requests_workflow_step",
)
# Indexes that aren't a constraint's (a unique constraint's and the key's are renamed with it).
INDEXES = (
    "ix_schema_change_requests_requested_by_run",
    "ix_schema_change_requests_workflow_step_id",
)

# The expiry approvals made before expiry times were recorded had: the old setting's default.
OLD_EXPIRY = "interval '7 days'"


def _rename(old_table: str, new_table: str, old: str, new: str, comment: str) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{old_table} RENAME TO {new_table}")
    for name in CONSTRAINTS:
        op.execute(
            f"ALTER TABLE {SCHEMA}.{new_table}"
            f" RENAME CONSTRAINT {name.replace(OLD, old)} TO {name.replace(OLD, new)}"
        )
    for name in INDEXES:
        op.execute(
            f"ALTER INDEX {SCHEMA}.{name.replace(OLD, old)} RENAME TO {name.replace(OLD, new)}"
        )
    op.execute(f"COMMENT ON TABLE {SCHEMA}.{new_table} IS '{comment}'")


def upgrade() -> None:
    op.drop_index(
        "ix_workflow_run_steps_waiting_on_request_id", "workflow_run_steps", schema=SCHEMA
    )
    op.drop_constraint(
        "fk_workflow_run_steps_waiting_on_request_id", "workflow_run_steps", schema=SCHEMA
    )
    op.drop_column("workflow_run_steps", "waiting_on_request_id", schema=SCHEMA)

    op.execute(
        f"UPDATE {SCHEMA}.{OLD} SET expires_at = created_at + {OLD_EXPIRY} WHERE expires_at IS NULL"
    )
    op.alter_column(OLD, "expires_at", nullable=False, schema=SCHEMA)

    _rename(
        OLD,
        NEW,
        OLD,
        NEW,
        "What members must decide before a run carries on: agents'' schema changes and"
        " workflow approval steps",
    )
    op.execute(f"ALTER TYPE {SCHEMA}.schema_change_status RENAME TO approval_status")


def downgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.approval_status RENAME TO schema_change_status")
    _rename(
        NEW,
        OLD,
        NEW,
        OLD,
        "Agents'' requests to change the data schema, and what was decided",
    )
    op.alter_column(OLD, "expires_at", nullable=True, schema=SCHEMA)

    op.add_column(
        "workflow_run_steps",
        sa.Column("waiting_on_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_workflow_run_steps_waiting_on_request_id",
        "workflow_run_steps",
        OLD,
        ["waiting_on_request_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workflow_run_steps_waiting_on_request_id",
        "workflow_run_steps",
        ["waiting_on_request_id"],
        schema=SCHEMA,
    )
