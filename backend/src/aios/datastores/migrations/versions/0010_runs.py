"""runs

The organisation's runs move into its store: agent runs (`agent_runs`, each holding its turn
lease), the sandbox each one's Python runs in (`sandbox_sessions`), workflow runs
(`workflow_runs`) and their steps (`workflow_run_steps`). Runs name the agent or workflow and the
version they ran by foreign key, as they're in the same store now. What they name that's still on
the platform (the member who started them, the schedule, the schema change request a step waits
on) is by id, with no foreign key.

Each run's conversation is its checkpointer thread, `agent-run-<id>`, in the checkpointer's own
tables, which are made here too, in the store's `langgraph` schema (`aios.checkpoints.set_up_store`,
run again each time the store is migrated, so a newer LangGraph's migrations reach every store).
The read-write role finds them through its search path in the store, which the provisioner sets
(`aios.datastores.provision.upgrade_store`): the tree doesn't know the store's role names.

Nothing is copied from the platform, whose tables the same release drops: development data is
rebuilt with `make seed`.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey(f"{SCHEMA}.agents.id"), nullable=False),
        sa.Column(
            "agent_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_versions.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("trigger_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", name="agent_run_status", schema=SCHEMA),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        schema=SCHEMA,
        comment="Conversations with agents: each run's history is its checkpointer thread",
    )
    op.create_index("ix_agent_runs_agent_id", "agent_runs", ["agent_id"], schema=SCHEMA)
    op.create_index(
        "ix_agent_runs_agent_version_id", "agent_runs", ["agent_version_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_agent_runs_running_heartbeat",
        "agent_runs",
        ["heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running' AND heartbeat_at IS NOT NULL"),
    )
    op.create_table(
        "sandbox_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ref", sa.String(512), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("secret", sa.String(255), nullable=False),
        sa.Column("replaced", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.UniqueConstraint("agent_run_id", name="sandbox_sessions_agent_run_id_key"),
        schema=SCHEMA,
        comment="The sandbox each agent run's Python runs in",
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_id", sa.Integer(), sa.ForeignKey(f"{SCHEMA}.workflows.id"), nullable=False
        ),
        sa.Column(
            "workflow_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.workflow_versions.id"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("trigger_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "waiting",
                "succeeded",
                "failed",
                name="workflow_run_status",
                schema=SCHEMA,
            ),
            nullable=False,
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waited_seconds", sa.Double(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
        comment="Runs of workflows: each walks its pinned version's graph",
    )
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"], schema=SCHEMA)
    op.create_index(
        "ix_workflow_runs_workflow_version_id",
        "workflow_runs",
        ["workflow_version_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="workflow_node_kind", schema=SCHEMA, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "waiting",
                "succeeded",
                "failed",
                name="workflow_step_status",
                schema=SCHEMA,
            ),
            nullable=False,
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("waiting_on_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
        comment="Each workflow run's steps: a node of its graph, at any depth",
    )
    for column in ("run_id", "agent_run_id", "waiting_on_request_id"):
        op.create_index(
            f"ix_workflow_run_steps_{column}", "workflow_run_steps", [column], schema=SCHEMA
        )

    # The checkpointer's tables, where the runs' conversations are.
    from aios.checkpoints import set_up_store

    set_up_store(op.get_bind().connection.driver_connection)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
    for table in ("workflow_run_steps", "workflow_runs", "sandbox_sessions", "agent_runs"):
        op.drop_table(table, schema=SCHEMA)
    for enum in ("workflow_step_status", "workflow_run_status", "agent_run_status"):
        op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.{enum}")
