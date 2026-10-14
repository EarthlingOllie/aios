"""triggers

Triggers start agents and workflows without a user in the loop. The first kind is a schedule: a
cron expression in a timezone, polled by the API's scheduler (`aios.scheduler`). Runs record the
trigger that started them.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 21:08:23.546987
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "triggers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("schedule", name="trigger_kind"), nullable=False),
        sa.Column(
            "target_kind", sa.Enum("agent", "workflow", name="trigger_target_kind"), nullable=False
        ),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("input", sa.Text(), server_default="", nullable=False),
        sa.Column("cron", sa.String(length=255), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status",
            sa.Enum("running", "succeeded", "failed", name="trigger_status"),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_triggers_target",
        ),
        sa.CheckConstraint(
            "kind <> 'schedule' OR (cron IS NOT NULL AND timezone IS NOT NULL)",
            name="ck_triggers_schedule",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_triggers_agent_id"), "triggers", ["agent_id"], unique=False)
    op.create_index(
        "ix_triggers_due",
        "triggers",
        ["next_run_at"],
        unique=False,
        postgresql_where=sa.text("next_run_at IS NOT NULL"),
    )
    op.create_index(op.f("ix_triggers_owner_id"), "triggers", ["owner_id"], unique=False)
    op.create_index(op.f("ix_triggers_workflow_id"), "triggers", ["workflow_id"], unique=False)
    op.create_foreign_key(
        "fk_triggers_last_run_id",
        "triggers",
        "agent_runs",
        ["last_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("agent_runs", sa.Column("trigger_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_agent_runs_trigger_id"), "agent_runs", ["trigger_id"], unique=False)
    op.create_foreign_key(
        "agent_runs_trigger_id_fkey",
        "agent_runs",
        "triggers",
        ["trigger_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("agent_runs_trigger_id_fkey", "agent_runs", type_="foreignkey")
    op.drop_constraint("fk_triggers_last_run_id", "triggers", type_="foreignkey")
    op.drop_index(op.f("ix_agent_runs_trigger_id"), table_name="agent_runs")
    op.drop_column("agent_runs", "trigger_id")
    op.drop_index(op.f("ix_triggers_workflow_id"), table_name="triggers")
    op.drop_index(op.f("ix_triggers_owner_id"), table_name="triggers")
    op.drop_index(
        "ix_triggers_due",
        table_name="triggers",
        postgresql_where=sa.text("next_run_at IS NOT NULL"),
    )
    op.drop_index(op.f("ix_triggers_agent_id"), table_name="triggers")
    op.drop_table("triggers")
    for name in ("trigger_status", "trigger_target_kind", "trigger_kind"):
        sa.Enum(name=name).drop(op.get_bind())
