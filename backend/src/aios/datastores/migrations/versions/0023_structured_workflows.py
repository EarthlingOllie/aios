"""structured workflows: schemas, templates and conditions

`aios.workflow_versions` gain an input and an output JSON Schema, `aios.workflow_nodes` an input
schema, an output schema and an input template, and `aios.workflow_edges` a JSONLogic condition
(`aios.workflows.structured`). All optional: a workflow without them runs as before.

`aios.workflow_runs` gain their input and output as JSON beside the text ones. Steps gain their
input as JSON, the message their agent's model was sent (predecessors' outputs wrapped as
untrusted), a structured output, their outgoing edges' evaluated conditions, and the status
"skipped" (no active path reached the node).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"

COLUMNS = {
    "workflow_versions": ("input_schema", "output_schema"),
    "workflow_nodes": ("input_schema", "output_schema", "input_template"),
    "workflow_edges": ("condition",),
    "workflow_runs": ("input_json", "output_json"),
    "workflow_run_steps": ("input_json", "output_json", "conditions"),
}


def upgrade() -> None:
    op.execute(f"ALTER TYPE {SCHEMA}.workflow_step_status ADD VALUE IF NOT EXISTS 'skipped'")
    for table, columns in COLUMNS.items():
        for column in columns:
            op.add_column(
                table,
                sa.Column(column, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
                schema=SCHEMA,
            )
    op.add_column(
        "workflow_run_steps", sa.Column("message", sa.Text(), nullable=True), schema=SCHEMA
    )


def downgrade() -> None:
    # A value can't be dropped from an enum: skipped steps fail, and `skipped` stays unused.
    op.execute(
        f"UPDATE {SCHEMA}.workflow_run_steps SET status = 'failed', error = 'Skipped'"
        " WHERE status::text = 'skipped'"
    )
    op.drop_column("workflow_run_steps", "message", schema=SCHEMA)
    for table, columns in COLUMNS.items():
        for column in columns:
            op.drop_column(table, column, schema=SCHEMA)
