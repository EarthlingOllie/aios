"""approvals: schema changes and workflow approval steps as one concept

`aios.schema_change_requests` becomes the table of approvals (`aios.approvals`), still under its
old name until the rename: it gains a kind (`schema_change`, the default, or `workflow_step`),
the role that decides a workflow step's (editor or admin), when it expires, the workflow step
waiting on it and that step's input. The schema-change-only columns (`ops`, `sql`, `summary`,
`base_revision`) become optional, and a schema change must have them.

`aios.workflow_nodes` gain the approval node: the kind `approval`, which pins nothing, with its
prompt template, approver role and optional expiry in days. `aios.workflow_run_steps` gain the
approval they wait on (`approval_id`), which replaces `waiting_on_request_id` (kept, unused,
until the rename drops it): copied over here.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
REQUESTS = "schema_change_requests"
SCHEMA_CHANGE_ONLY = ("ops", "sql", "summary", "base_revision")

# The value is new in this transaction, so the constraints compare the kind as text.
OLD_TARGET = (
    "(kind = 'agent' AND agent_version_id IS NOT NULL AND subworkflow_version_id IS NULL)"
    " OR (kind = 'workflow' AND subworkflow_version_id IS NOT NULL"
    " AND agent_version_id IS NULL)"
)
TARGET = (
    "(kind::text = 'agent' AND agent_version_id IS NOT NULL AND subworkflow_version_id IS NULL)"
    " OR (kind::text = 'workflow' AND subworkflow_version_id IS NOT NULL"
    " AND agent_version_id IS NULL)"
    " OR (kind::text = 'approval' AND agent_version_id IS NULL"
    " AND subworkflow_version_id IS NULL)"
)
APPROVAL_NODE = (
    "kind::text = 'approval' AND prompt_template IS NOT NULL AND approver_role IS NOT NULL"
    " OR kind::text <> 'approval' AND prompt_template IS NULL AND approver_role IS NULL"
    " AND expiry_days IS NULL"
)


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.workflow_node_kind ADD VALUE IF NOT EXISTS 'approval'")

    # Approvals.
    op.add_column(
        REQUESTS,
        sa.Column("kind", sa.Text(), server_default="schema_change", nullable=False),
        schema=SCHEMA,
    )
    op.add_column(REQUESTS, sa.Column("approver_role", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(
        REQUESTS,
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        REQUESTS, sa.Column("workflow_step_id", sa.Integer(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        REQUESTS,
        sa.Column("input", postgresql.JSONB(none_as_null=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_schema_change_requests_workflow_step_id",
        REQUESTS,
        "workflow_run_steps",
        ["workflow_step_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_schema_change_requests_workflow_step_id",
        REQUESTS,
        ["workflow_step_id"],
        schema=SCHEMA,
    )
    for column in SCHEMA_CHANGE_ONLY:
        op.alter_column(REQUESTS, column, nullable=True, schema=SCHEMA)
    op.create_check_constraint(
        "ck_schema_change_requests_kind",
        REQUESTS,
        "kind IN ('schema_change', 'workflow_step')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_schema_change_requests_approver_role",
        REQUESTS,
        "approver_role IN ('editor', 'admin')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_schema_change_requests_schema_change",
        REQUESTS,
        "kind <> 'schema_change' OR (ops IS NOT NULL AND sql IS NOT NULL"
        " AND summary IS NOT NULL AND base_revision IS NOT NULL)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_schema_change_requests_workflow_step",
        REQUESTS,
        "kind <> 'workflow_step' OR approver_role IS NOT NULL",
        schema=SCHEMA,
    )

    # The approval node.
    op.add_column(
        "workflow_nodes", sa.Column("prompt_template", sa.Text(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "workflow_nodes", sa.Column("approver_role", sa.Text(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "workflow_nodes", sa.Column("expiry_days", sa.Integer(), nullable=True), schema=SCHEMA
    )
    op.drop_constraint("ck_workflow_nodes_target", "workflow_nodes", schema=SCHEMA)
    op.create_check_constraint("ck_workflow_nodes_target", "workflow_nodes", TARGET, schema=SCHEMA)
    op.create_check_constraint(
        "ck_workflow_nodes_approval", "workflow_nodes", APPROVAL_NODE, schema=SCHEMA
    )
    op.create_check_constraint(
        "ck_workflow_nodes_approver_role",
        "workflow_nodes",
        "approver_role IN ('editor', 'admin')",
        schema=SCHEMA,
    )

    # The approval a step waits on.
    op.add_column(
        "workflow_run_steps",
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_workflow_run_steps_approval_id",
        "workflow_run_steps",
        REQUESTS,
        ["approval_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workflow_run_steps_approval_id", "workflow_run_steps", ["approval_id"], schema=SCHEMA
    )
    op.execute(
        f"UPDATE {SCHEMA}.workflow_run_steps SET approval_id = waiting_on_request_id"
        " WHERE waiting_on_request_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE {SCHEMA}.workflow_run_steps SET waiting_on_request_id = approval_id"
        f" WHERE approval_id IN (SELECT id FROM {SCHEMA}.{REQUESTS}"
        " WHERE kind = 'schema_change')"
    )
    op.drop_index("ix_workflow_run_steps_approval_id", "workflow_run_steps", schema=SCHEMA)
    op.drop_constraint("fk_workflow_run_steps_approval_id", "workflow_run_steps", schema=SCHEMA)
    op.drop_column("workflow_run_steps", "approval_id", schema=SCHEMA)

    # A value can't be dropped from an enum: approval nodes go, with the versions that have
    # them, and `approval` stays unused. Their runs' steps fail first.
    op.execute(
        f"DELETE FROM {SCHEMA}.workflow_edges e USING {SCHEMA}.workflow_nodes n"
        " WHERE n.kind::text = 'approval' AND e.workflow_version_id = n.workflow_version_id"
        " AND n.key IN (e.source, e.target)"
    )
    op.execute(f"DELETE FROM {SCHEMA}.workflow_nodes WHERE kind::text = 'approval'")
    op.drop_constraint("ck_workflow_nodes_approver_role", "workflow_nodes", schema=SCHEMA)
    op.drop_constraint("ck_workflow_nodes_approval", "workflow_nodes", schema=SCHEMA)
    op.drop_constraint("ck_workflow_nodes_target", "workflow_nodes", schema=SCHEMA)
    op.create_check_constraint(
        "ck_workflow_nodes_target", "workflow_nodes", OLD_TARGET, schema=SCHEMA
    )
    for column in ("expiry_days", "approver_role", "prompt_template"):
        op.drop_column("workflow_nodes", column, schema=SCHEMA)

    # Workflow step approvals go; the schema change columns are required again.
    op.execute(f"DELETE FROM {SCHEMA}.{REQUESTS} WHERE kind <> 'schema_change'")
    for name in ("workflow_step", "schema_change", "approver_role", "kind"):
        op.drop_constraint(f"ck_schema_change_requests_{name}", REQUESTS, schema=SCHEMA)
    for column in SCHEMA_CHANGE_ONLY:
        op.alter_column(REQUESTS, column, nullable=False, schema=SCHEMA)
    op.drop_index("ix_schema_change_requests_workflow_step_id", REQUESTS, schema=SCHEMA)
    op.drop_constraint("fk_schema_change_requests_workflow_step_id", REQUESTS, schema=SCHEMA)
    for column in ("input", "workflow_step_id", "expires_at", "approver_role", "kind"):
        op.drop_column(REQUESTS, column, schema=SCHEMA)
