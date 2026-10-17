"""run history

The run history export's tables, in a schema of their own (`history`): a copy of the owner's
agent runs and their messages, keyed by the platform's ids (`aios.datastores.history`).

The first revision of the tree's `history` branch, which a store gets only once its owner
switches the export on (`aios.datastores.provision`): it starts from nothing, so it runs on a
store at any revision of the `aios` line. The store's roles get the same privileges on these
tables as on `aios`'s, through the owner's default privileges (`provision._set_up_database`).

Revision ID: h0001
Revises:
Create Date: 2026-10-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "h0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("history",)
depends_on: str | Sequence[str] | None = None

SCHEMA = "history"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table(
        "agent_runs",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=False,
            nullable=False,
            comment="The run's id on the platform.",
        ),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column(
            "agent_version", sa.Integer(), nullable=False, comment="The agent version it ran."
        ),
        sa.Column(
            "model",
            sa.Text(),
            nullable=True,
            comment="The version's model; null means the deployment's default.",
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="The user who ran it."),
        sa.Column(
            "trigger_id",
            sa.BigInteger(),
            nullable=True,
            comment="The schedule that started it, if one did.",
        ),
        sa.Column(
            "workflow_run_id",
            sa.BigInteger(),
            nullable=True,
            comment="The workflow run whose agent node it was, if any.",
        ),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When its last turn ended.",
        ),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
        comment="A copy of an agent run (one conversation with an agent), from the platform.",
    )
    op.create_table(
        "messages",
        sa.Column("agent_run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "position", sa.Integer(), nullable=False, comment="Its place in the run, from 0."
        ),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, comment="user, assistant, tool or system."),
        sa.Column("text", sa.Text(), nullable=False, comment="Its text, without tool calls."),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="An assistant message's tool calls: [{id, name, args}].",
        ),
        sa.Column(
            "tool_call_id",
            sa.Text(),
            nullable=True,
            comment="A tool message's call, in its run's tool_calls.",
        ),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=True, comment="A tool message that failed."),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="An assistant message's token usage: {input_tokens, output_tokens, ...}.",
        ),
        sa.Column("model", sa.Text(), nullable=True, comment="The model that wrote it."),
        sa.ForeignKeyConstraint(["agent_run_id"], [f"{SCHEMA}.agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_run_id", "position"),
        schema=SCHEMA,
        comment="The messages of the agent runs in history.agent_runs, in order.",
    )


def downgrade() -> None:
    op.drop_table("messages", schema=SCHEMA)
    op.drop_table("agent_runs", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
