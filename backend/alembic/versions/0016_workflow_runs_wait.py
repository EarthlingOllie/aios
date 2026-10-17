"""workflow runs wait

Workflow runs pause while an agent node waits for a schema change request: runs and steps can be
`waiting`, a waiting step records the request, and a run records how long it has waited (which
doesn't count towards its time limit).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE workflow_run_status ADD VALUE 'waiting' AFTER 'running'")
    op.execute("ALTER TYPE workflow_step_status ADD VALUE 'waiting' AFTER 'running'")
    op.add_column(
        "workflow_runs", sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_runs",
        sa.Column("waited_seconds", sa.Double(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "workflow_run_steps", sa.Column("waiting_on_request_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        op.f("ix_workflow_run_steps_waiting_on_request_id"),
        "workflow_run_steps",
        ["waiting_on_request_id"],
    )
    op.create_foreign_key(
        "workflow_run_steps_waiting_on_request_id_fkey",
        "workflow_run_steps",
        "schema_change_requests",
        ["waiting_on_request_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _drop_waiting(table: str, type_name: str, values: list[str]) -> None:
    # Postgres can't drop an enum value: waiting rows fail, and the type is made again without it.
    op.execute(f"UPDATE {table} SET status = 'failed' WHERE status = 'waiting'")
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN status TYPE {type_name} USING status::text::{type_name}"
    )
    op.execute(f"DROP TYPE {type_name}_old")


def downgrade() -> None:
    op.drop_constraint(
        "workflow_run_steps_waiting_on_request_id_fkey", "workflow_run_steps", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_workflow_run_steps_waiting_on_request_id"), table_name="workflow_run_steps"
    )
    op.drop_column("workflow_run_steps", "waiting_on_request_id")
    op.drop_column("workflow_runs", "waited_seconds")
    op.drop_column("workflow_runs", "waiting_since")
    _drop_waiting("workflow_run_steps", "workflow_step_status", ["running", "succeeded", "failed"])
    _drop_waiting(
        "workflow_runs", "workflow_run_status", ["queued", "running", "succeeded", "failed"]
    )
