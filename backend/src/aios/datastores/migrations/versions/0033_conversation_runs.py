"""Conversation runs: an agent keeps one run per chat

Agent runs gain the chat they're the agent's conversation in (`agent_runs.chat_id`), null for
runs with no conversation (started by hand, by triggers, by `start_workflow`, or as workflow
nodes), and a partial unique index allowing one run per agent and chat. Each existing chat's
latest mention run of each agent becomes its conversation run there.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("chat_id", sa.Integer(), nullable=True), schema=SCHEMA)
    op.create_foreign_key(
        "fk_agent_runs_chat_id",
        "agent_runs",
        "chats",
        ["chat_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.execute(
        f"""
        UPDATE {SCHEMA}.agent_runs AS r SET chat_id = latest.chat_id
        FROM (
            SELECT DISTINCT ON (i.agent_id, i.chat_id) i.agent_run_id, i.chat_id
            FROM {SCHEMA}.invocations AS i
            WHERE i.chat_id IS NOT NULL AND i.agent_run_id IS NOT NULL
            ORDER BY i.agent_id, i.chat_id, i.id DESC
        ) AS latest
        WHERE r.id = latest.agent_run_id
        """
    )
    op.create_index(
        "uq_agent_runs_agent_id_chat_id",
        "agent_runs",
        ["agent_id", "chat_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("chat_id IS NOT NULL"),
    )
    op.create_index("ix_agent_runs_chat_id", "agent_runs", ["chat_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_chat_id", "agent_runs", schema=SCHEMA)
    op.drop_index("uq_agent_runs_agent_id_chat_id", "agent_runs", schema=SCHEMA)
    op.drop_constraint("fk_agent_runs_chat_id", "agent_runs", schema=SCHEMA, type_="foreignkey")
    op.drop_column("agent_runs", "chat_id", schema=SCHEMA)
