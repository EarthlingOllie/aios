"""an invocation for every run, and turns as records

Every run now has an invocation recording why it ran (`aios.invocations`), not only a mention's:
a schedule's names its trigger (`trigger_id`), and a workflow agent node's is a child of its
workflow run's (`parent_id`), with the chain of its ancestors' ids (`ancestor_ids`, the root
first) and its depth (0 for one with no parent), so later phases can stop loops.

Each turn of an agent run is a row of `turns`: its run, the invocation it ran for, the agent
version it ran on, how it went (status and error), when it began and ended, and its model calls,
tool calls and tokens. A run's status, times and counters are its turns' (its latest turn's
status and times, the counters summed over all of them), so the run's own `status`,
`started_at`, `finished_at`, `tool_call_count`, `input_tokens` and `output_tokens` go.

A mention's child (an agent node of a mentioned workflow's run) takes the mention as its cause,
but only the root names the chat and message (`ck_invocations_mention`).

A `reporting` schema holds read-only views of runs (`reporting.runs`, agent and workflow runs)
and turns (`reporting.turns`) for agents to query: identifiers, counts, times and statuses only,
never a message's text (nor a run's title, input, output or error, which hold some). The
read-only role reads them through the store's default privileges; the provisioner keeps the
read-write role from writing through them (`aios.datastores.provision.upgrade_store`).

Nothing is copied: development data is rebuilt with `make seed`.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
REPORTING = "reporting"

# A run's derived status, times and counters (as `aios.turns` derives them).
RUNS_VIEW = f"""
CREATE VIEW {REPORTING}.runs AS
SELECT 'agent'::text AS kind,
       r.id AS run_id,
       r.agent_id,
       r.agent_version_id,
       NULL::integer AS workflow_id,
       NULL::integer AS workflow_version_id,
       i.id AS invocation_id,
       i.cause::text AS cause,
       i.parent_id AS parent_invocation_id,
       i.depth,
       r.trigger_id,
       r.created_by,
       latest.status::text AS status,
       r.created_at,
       first_turn.started_at,
       latest.finished_at,
       coalesce(totals.turns, 0) AS turns,
       coalesce(totals.model_calls, 0) AS model_calls,
       coalesce(totals.tool_calls, 0) AS tool_calls,
       coalesce(totals.input_tokens, 0) AS input_tokens,
       coalesce(totals.output_tokens, 0) AS output_tokens
FROM {SCHEMA}.agent_runs r
LEFT JOIN {SCHEMA}.invocations i ON i.agent_run_id = r.id
LEFT JOIN LATERAL (
    SELECT t.status, t.finished_at FROM {SCHEMA}.turns t
    WHERE t.agent_run_id = r.id ORDER BY t.id DESC LIMIT 1
) latest ON true
LEFT JOIN LATERAL (
    SELECT t.started_at FROM {SCHEMA}.turns t
    WHERE t.agent_run_id = r.id ORDER BY t.id LIMIT 1
) first_turn ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS turns,
           sum(t.model_calls) AS model_calls,
           sum(t.tool_calls) AS tool_calls,
           sum(t.input_tokens) AS input_tokens,
           sum(t.output_tokens) AS output_tokens
    FROM {SCHEMA}.turns t WHERE t.agent_run_id = r.id
) totals ON true
UNION ALL
SELECT 'workflow'::text,
       w.id,
       NULL::integer,
       NULL::integer,
       w.workflow_id,
       w.workflow_version_id,
       i.id,
       i.cause::text,
       i.parent_id,
       i.depth,
       w.trigger_id,
       w.created_by,
       w.status::text,
       w.created_at,
       w.started_at,
       w.finished_at,
       coalesce(nodes.turns, 0),
       coalesce(nodes.model_calls, 0),
       coalesce(nodes.tool_calls, 0),
       coalesce(nodes.input_tokens, 0),
       coalesce(nodes.output_tokens, 0)
FROM {SCHEMA}.workflow_runs w
LEFT JOIN {SCHEMA}.invocations i ON i.workflow_run_id = w.id
LEFT JOIN LATERAL (
    SELECT count(*) AS turns,
           sum(t.model_calls) AS model_calls,
           sum(t.tool_calls) AS tool_calls,
           sum(t.input_tokens) AS input_tokens,
           sum(t.output_tokens) AS output_tokens
    FROM {SCHEMA}.workflow_run_steps s
    JOIN {SCHEMA}.turns t ON t.agent_run_id = s.agent_run_id
    WHERE s.run_id = w.id
) nodes ON true
"""

TURNS_VIEW = f"""
CREATE VIEW {REPORTING}.turns AS
SELECT t.id AS turn_id,
       t.agent_run_id AS run_id,
       r.agent_id,
       t.agent_version_id,
       t.invocation_id,
       i.cause::text AS cause,
       t.status::text AS status,
       t.started_at,
       t.finished_at,
       extract(epoch FROM t.finished_at - t.started_at)::double precision AS seconds,
       t.model_calls,
       t.tool_calls,
       t.input_tokens,
       t.output_tokens
FROM {SCHEMA}.turns t
JOIN {SCHEMA}.agent_runs r ON r.id = t.agent_run_id
LEFT JOIN {SCHEMA}.invocations i ON i.id = t.invocation_id
"""


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    # Invocations: who or what caused each, and where it sits in a chain.
    op.add_column(
        "invocations",
        sa.Column(
            "trigger_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.triggers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "invocations",
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.invocations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "invocations",
        sa.Column(
            "ancestor_ids",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'::integer[]"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "invocations",
        sa.Column("depth", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_invocations_depth",
        "invocations",
        "depth = cardinality(ancestor_ids)",
        schema=SCHEMA,
    )
    # A mention's child (a workflow agent node's, under a mentioned workflow's run) takes its
    # cause, but no chat follows it: only one with no parent names the chat and message.
    op.drop_constraint("ck_invocations_mention", "invocations", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_invocations_mention",
        "invocations",
        "cause <> 'mention' OR depth > 0 OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
        schema=SCHEMA,
    )
    for column in ("trigger_id", "parent_id"):
        op.create_index(f"ix_invocations_{column}", "invocations", [column], schema=SCHEMA)
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.invocations IS"
        " 'Why each run ran: a mention, a schedule, or a person starting it by hand'"
    )

    # Turns.
    turn_status = sa.Enum("running", "succeeded", "failed", name="turn_status", schema=SCHEMA)
    op.create_table(
        "turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invocation_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.invocations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "agent_version_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agent_versions.id"),
            nullable=False,
        ),
        sa.Column("status", turn_status, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
        comment="Each turn of an agent run: what it ran on, how it went, and what it cost",
    )
    op.create_index("ix_turns_agent_run_id_id", "turns", ["agent_run_id", "id"], schema=SCHEMA)
    op.create_index("ix_turns_invocation_id", "turns", ["invocation_id"], schema=SCHEMA)
    op.create_index("ix_turns_agent_version_id", "turns", ["agent_version_id"], schema=SCHEMA)

    # A run's status, times and counters are its turns' now.
    op.drop_index("ix_agent_runs_running_heartbeat", "agent_runs", schema=SCHEMA)
    for column in (
        "status",
        "started_at",
        "finished_at",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
    ):
        op.drop_column("agent_runs", column, schema=SCHEMA)
    op.execute(f"DROP TYPE {SCHEMA}.agent_run_status")
    op.create_index(
        "ix_agent_runs_heartbeat",
        "agent_runs",
        ["heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("heartbeat_at IS NOT NULL"),
    )

    # What agents read of them.
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {REPORTING}")
    op.execute(RUNS_VIEW)
    op.execute(TURNS_VIEW)
    op.execute(
        f"COMMENT ON VIEW {REPORTING}.runs IS 'Agent and workflow runs: why each ran, how it"
        " went and what it cost (summed over its turns, or its agent nodes'' turns)'"
    )
    op.execute(
        f"COMMENT ON VIEW {REPORTING}.turns IS 'Each turn of an agent run: the version it ran"
        " on, how it went, how long it took and what it cost'"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {REPORTING}.turns")
    op.execute(f"DROP VIEW IF EXISTS {REPORTING}.runs")
    # Only if nothing else is reported there.
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n"
        f" ON n.oid = c.relnamespace WHERE n.nspname = '{REPORTING}')"
        f" THEN DROP SCHEMA IF EXISTS {REPORTING}; END IF; END $$"
    )
    op.drop_index("ix_agent_runs_heartbeat", "agent_runs", schema=SCHEMA)
    status = sa.Enum("running", "succeeded", "failed", name="agent_run_status", schema=SCHEMA)
    status.create(op.get_bind())
    op.add_column("agent_runs", sa.Column("status", status, nullable=True), schema=SCHEMA)
    for column in ("started_at", "finished_at"):
        op.add_column(
            "agent_runs",
            sa.Column(column, sa.DateTime(timezone=True), nullable=True),
            schema=SCHEMA,
        )
    for column in ("tool_call_count", "input_tokens", "output_tokens"):
        op.add_column("agent_runs", sa.Column(column, sa.Integer(), nullable=True), schema=SCHEMA)
    op.create_index(
        "ix_agent_runs_running_heartbeat",
        "agent_runs",
        ["heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'running' AND heartbeat_at IS NOT NULL"),
    )
    op.drop_table("turns", schema=SCHEMA)
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.turn_status")
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.invocations IS 'Why each run ran: so far, a mention in a chat'"
    )
    for column in ("trigger_id", "parent_id"):
        op.drop_index(f"ix_invocations_{column}", "invocations", schema=SCHEMA)
    op.drop_constraint("ck_invocations_depth", "invocations", schema=SCHEMA, type_="check")
    op.drop_constraint("ck_invocations_mention", "invocations", schema=SCHEMA, type_="check")
    op.execute(f"DELETE FROM {SCHEMA}.invocations WHERE depth > 0")
    op.create_check_constraint(
        "ck_invocations_mention",
        "invocations",
        "cause <> 'mention' OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
        schema=SCHEMA,
    )
    for column in ("depth", "ancestor_ids", "parent_id", "trigger_id"):
        op.drop_column("invocations", column, schema=SCHEMA)
