"""workflow runs record the schedule that started them

A run says who started it: the member (`created_by`), or, for a schedule's run, which has no
member, the schedule. Agent runs have kept their trigger since schedules came (0010); workflow
runs kept only the trigger's link to its latest run (`triggers.last_workflow_run_id`), so a
workflow run started by a schedule couldn't be told from one whose member is unknown. They get
the same `trigger_id`, null once the trigger is deleted.

Existing runs are filled in where it's known: each trigger's latest workflow run. Earlier
scheduled runs stay null, as runs from before agent runs kept theirs do.

Revision ID: 0047
Revises: 0035
Create Date: 2026-10-19 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("trigger_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_workflow_runs_trigger_id"), "workflow_runs", ["trigger_id"], unique=False
    )
    op.create_foreign_key(
        "workflow_runs_trigger_id_fkey",
        "workflow_runs",
        "triggers",
        ["trigger_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE workflow_runs SET trigger_id = triggers.id
        FROM triggers WHERE triggers.last_workflow_run_id = workflow_runs.id
        """
    )


def downgrade() -> None:
    op.drop_constraint("workflow_runs_trigger_id_fkey", "workflow_runs", type_="foreignkey")
    op.drop_index(op.f("ix_workflow_runs_trigger_id"), table_name="workflow_runs")
    op.drop_column("workflow_runs", "trigger_id")
