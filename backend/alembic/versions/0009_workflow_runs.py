"""workflow runs

Running a workflow: a run pins the workflow version it started on, and records a step for each
node as it starts, at any depth. An agent node's step points at the agent run holding its
conversation.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("workflow_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "succeeded", "failed", name="workflow_run_status"),
            nullable=False,
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_runs_workflow_id"), "workflow_runs", ["workflow_id"], unique=False
    )
    op.create_index(
        op.f("ix_workflow_runs_workflow_version_id"),
        "workflow_runs",
        ["workflow_version_id"],
        unique=False,
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("path", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM("agent", "workflow", name="workflow_node_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", name="workflow_step_status"),
            nullable=False,
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_run_steps_agent_run_id"),
        "workflow_run_steps",
        ["agent_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_run_steps_run_id"), "workflow_run_steps", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_workflow_run_steps_run_id"), table_name="workflow_run_steps")
    op.drop_index(op.f("ix_workflow_run_steps_agent_run_id"), table_name="workflow_run_steps")
    op.drop_table("workflow_run_steps")
    op.drop_index(op.f("ix_workflow_runs_workflow_version_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_workflow_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
    sa.Enum(name="workflow_step_status").drop(op.get_bind())
    sa.Enum(name="workflow_run_status").drop(op.get_bind())
