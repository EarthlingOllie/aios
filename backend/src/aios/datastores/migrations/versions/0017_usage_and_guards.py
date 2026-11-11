"""the usage ledger, invocable-by and limits, and refused invocations

`aios.usage` is the usage ledger (`aios.usage`): one row per billable unit (a model call, an
embedding batch, a provider's web searches in one response, a tool call or a sandbox session's
seconds), with its model, tokens and quantity, its estimated cost in GBP (null for a model with
no price), its purpose, and what it's charged to: the agent or workflow, run, turn, invocation,
person and cause. `call_key` makes recording the same call again a no-op. It's never pruned.

`reporting.usage` is its view for agents: identifiers, counts, costs, times and kinds only.

Agents and workflows gain who may invoke them by mentioning them (`invocable_by`: everyone,
editors or admins; `aios.guards`) and their own caps (`limits`, `aios.limits`). An invocation
can be refused, with why (`refusal_reason`).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-15 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
REPORTING = "reporting"
KINDS = ("model_call", "embedding", "web_search", "tool_call", "sandbox_seconds")
PURPOSES = (
    "turn",
    "handoff",
    "handback",
    "compaction",
    "ingest",
    "search_query",
    "index_embedding",
    "insight",
)
INVOCABLE_BY = ("everyone", "editors", "admins")

USAGE_VIEW = f"""
CREATE VIEW {REPORTING}.usage AS
SELECT id AS usage_id, at, kind::text AS kind, purpose::text AS purpose, model,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, quantity, cost,
       agent_id, workflow_id, agent_run_id, workflow_run_id, turn_id, invocation_id, person_id,
       cause::text AS cause
FROM {SCHEMA}.usage
"""


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (
        ("usage_kind", KINDS),
        ("usage_purpose", PURPOSES),
        ("invocable_by", INVOCABLE_BY),
    ):
        sa.Enum(*values, name=name, schema=SCHEMA).create(bind)

    def enum(name: str, values) -> sa.Enum:
        return sa.Enum(*values, name=name, schema=SCHEMA, create_type=False)

    cause = sa.Enum(
        "mention", "schedule", "manual", name="invocation_cause", schema=SCHEMA, create_type=False
    )
    op.create_table(
        "usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was recorded.",
        ),
        sa.Column(
            "kind",
            enum("usage_kind", KINDS),
            nullable=False,
            comment="model_call, embedding, web_search, tool_call or sandbox_seconds.",
        ),
        sa.Column(
            "purpose",
            enum("usage_purpose", PURPOSES),
            nullable=False,
            comment="turn, handoff, handback, compaction, ingest, search_query, index_embedding"
            " or insight.",
        ),
        sa.Column("model", sa.Text(), nullable=True, comment="The model, as provider:model."),
        *(
            sa.Column(name, sa.BigInteger(), server_default=sa.text("0"), nullable=False)
            for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        ),
        sa.Column(
            "quantity",
            sa.Double(),
            server_default=sa.text("1"),
            nullable=False,
            comment="How many: calls, texts embedded, searches, or seconds.",
        ),
        sa.Column(
            "cost",
            sa.Double(),
            nullable=True,
            comment="Estimated cost in GBP; null for a model with no price.",
        ),
        sa.Column("agent_id", sa.Integer(), nullable=True, comment="aios.agents.id"),
        sa.Column("workflow_id", sa.Integer(), nullable=True, comment="aios.workflows.id"),
        sa.Column("agent_run_id", sa.Integer(), nullable=True, comment="aios.agent_runs.id"),
        sa.Column("workflow_run_id", sa.Integer(), nullable=True, comment="aios.workflow_runs.id"),
        sa.Column("turn_id", sa.Integer(), nullable=True, comment="aios.turns.id"),
        sa.Column("invocation_id", sa.Integer(), nullable=True, comment="aios.invocations.id"),
        sa.Column(
            "person_id", sa.Integer(), nullable=True, comment="The person charged (aios.people.id)."
        ),
        sa.Column("cause", cause, nullable=True, comment="mention, schedule or manual."),
        sa.Column("call_key", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_key", name="uq_usage_call_key"),
        schema=SCHEMA,
        comment="The usage ledger: one row per billable unit, priced in GBP. Never pruned.",
    )
    for column in ("at", "turn_id", "workflow_run_id"):
        op.create_index(f"ix_usage_{column}", "usage", [column], schema=SCHEMA)
    op.execute(USAGE_VIEW)
    op.execute(
        f"COMMENT ON VIEW {REPORTING}.usage IS 'The usage ledger: each billable unit (kind), why"
        " (purpose), its tokens or quantity and estimated cost in GBP, and what it is charged"
        " to: agent_id aios.agents.id, run ids aios.agent_runs.id and aios.workflow_runs.id,"
        " person_id aios.people.id'"
    )

    for table in ("agents", "workflows"):
        op.add_column(
            table,
            sa.Column(
                "invocable_by",
                enum("invocable_by", INVOCABLE_BY),
                server_default="everyone",
                nullable=False,
            ),
            schema=SCHEMA,
        )
        op.add_column(
            table,
            sa.Column(
                "limits",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            schema=SCHEMA,
        )

    op.execute(f"ALTER TYPE {SCHEMA}.invocation_status ADD VALUE IF NOT EXISTS 'refused'")
    op.add_column(
        "invocations", sa.Column("refusal_reason", sa.Text(), nullable=True), schema=SCHEMA
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.invocations WHERE status = 'refused'")
    op.drop_column("invocations", "refusal_reason", schema=SCHEMA)
    # Postgres can't drop an enum value: the type is rebuilt without it.
    op.execute(f"ALTER TYPE {SCHEMA}.invocation_status RENAME TO invocation_status_old")
    sa.Enum(
        "queued",
        "briefing",
        "running",
        "succeeded",
        "failed",
        name="invocation_status",
        schema=SCHEMA,
    ).create(op.get_bind())
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.ix_invocations_unfinished")
    op.execute(
        f"ALTER TABLE {SCHEMA}.invocations ALTER COLUMN status DROP DEFAULT,"
        f" ALTER COLUMN status TYPE {SCHEMA}.invocation_status"
        f" USING status::text::{SCHEMA}.invocation_status"
    )
    op.execute(
        f"CREATE INDEX ix_invocations_unfinished ON {SCHEMA}.invocations (chat_id)"
        " WHERE status IN ('queued', 'briefing', 'running')"
    )
    op.execute(f"DROP TYPE {SCHEMA}.invocation_status_old")
    for table in ("workflows", "agents"):
        op.drop_column(table, "limits", schema=SCHEMA)
        op.drop_column(table, "invocable_by", schema=SCHEMA)
    op.execute(f"DROP VIEW {REPORTING}.usage")
    op.drop_table("usage", schema=SCHEMA)
    for name in ("invocable_by", "usage_purpose", "usage_kind"):
        sa.Enum(name=name, schema=SCHEMA).drop(op.get_bind())
