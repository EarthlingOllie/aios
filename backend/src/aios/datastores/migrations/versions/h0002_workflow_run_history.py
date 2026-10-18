"""workflow run history

The run history export's copy of the owner's workflow runs and their steps, at every depth,
keyed by the platform's ids (`aios.datastores.history`). A step's agent run is the one of
`history.agent_runs` with its id: not a foreign key, as the two are exported apart, and either
may be in before the other.

Statuses are text, not an enum, so a status the platform adds later is copied as it is.

Revision ID: h0002
Revises: h0001
Create Date: 2026-10-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "h0002"
down_revision: str | None = "h0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "history"


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
            comment="The run's id on the platform.",
        ),
        sa.Column("workflow_id", sa.BigInteger(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column(
            "workflow_version",
            sa.Integer(),
            nullable=False,
            comment="The workflow version it ran.",
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="The user who ran it."),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="queued, running, succeeded or failed, as on the platform when exported.",
        ),
        sa.Column("input", sa.Text(), nullable=False, comment="What it was given."),
        sa.Column(
            "output", sa.Text(), nullable=True, comment="Its final nodes' outputs, once done."
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="Why it failed."),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
        comment="A copy of a workflow run, from the platform.",
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
            comment="The step's id on the platform.",
        ),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "path",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            comment="Node keys from the run's workflow down to the node: {review,critic} is "
            "node critic of the subworkflow at node review.",
        ),
        sa.Column("kind", sa.Text(), nullable=False, comment="agent or workflow."),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="running, succeeded or failed, as on the platform when exported.",
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "agent_run_id",
            sa.BigInteger(),
            nullable=True,
            comment="An agent node's conversation: history.agent_runs.id.",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], [f"{SCHEMA}.workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
        comment="The steps of the workflow runs in history.workflow_runs: a node, at any depth.",
    )
    op.create_index(
        "ix_workflow_run_steps_workflow_run_id",
        "workflow_run_steps",
        ["workflow_run_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("workflow_run_steps", schema=SCHEMA)
    op.drop_table("workflow_runs", schema=SCHEMA)
