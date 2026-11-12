"""insights: metrics-only views for the Insights page, and each turn's tool calls by tool

`aios.turn_tool_calls`: how many times each turn called each tool (the agent's own tools and
the provider's, e.g. web search), counted as its model calls are recorded (`aios.usage`), so
Insights can show the most used tools. Turns from before it have their total only
(`turns.tool_calls`).

The `insights` schema holds one view per Insights panel source (`aios.insights`), built on the
`reporting` views (store revisions 0013 and 0017) where they fit: identifiers, names of chats,
counts, times, statuses and costs only, never a message's text, a brief, a tool's input or
output, memory, synced text or an error. The read-only role reads them through the store's
default privileges; nobody writes through them (`aios.datastores.provision.upgrade_store`).

- `insights.agents`, `insights.workflows`: names.
- `insights.turns`: each turn, with the workflow its run is a node of and the person it's
  charged to; plus, as one untracked turn each (`tracked` false, counters null), every agent run
  with messages but no turns: from before turns were recorded, so counted but "not tracked".
- `insights.workflow_runs`: each workflow run, with its time running (`seconds`) excluding the
  time it waited for approvals (`waited_seconds`).
- `insights.workflow_steps`: each node of each run, with its time running excluding the time it
  waited for an approval: its own (an approval node's), or its agent's schema changes'.
- `insights.usage`: the usage ledger, as `reporting.usage`.
- `insights.tool_calls`: each turn's calls by tool.
- `insights.approvals`: each approval's kind, outcome and how long it waited to be decided.
- `insights.chats`, `insights.chat_messages`, `insights.mentions`: chats by name, their messages
  (who wrote them, how many they mention), and the runs mentions started, with how they went.

Any later migration changing a table these read must update them in the same migration.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"
INSIGHTS = "insights"

# The workflow whose run an agent run is a node of, if it is one.
_RUN_WORKFLOW = f"""
LEFT JOIN LATERAL (
    SELECT w.workflow_id FROM {SCHEMA}.workflow_run_steps st
    JOIN {SCHEMA}.workflow_runs w ON w.id = st.run_id
    WHERE st.agent_run_id = r.id LIMIT 1
) node ON true
"""

VIEWS: dict[str, tuple[str, str]] = {
    "agents": (
        f"SELECT a.id AS agent_id, a.name, a.builtin, a.archived_at FROM {SCHEMA}.agents a",
        "Each agent, by name.",
    ),
    "workflows": (
        f"SELECT w.id AS workflow_id, w.name, w.archived_at FROM {SCHEMA}.workflows w",
        "Each workflow, by name.",
    ),
    "turns": (
        f"""
SELECT t.turn_id, t.run_id, t.agent_id, node.workflow_id, t.cause,
       coalesce(i.requested_by, r.created_by) AS person_id,
       t.status, t.started_at, t.finished_at, t.seconds,
       t.model_calls, t.tool_calls, t.input_tokens, t.output_tokens,
       true AS tracked
FROM reporting.turns t
JOIN {SCHEMA}.agent_runs r ON r.id = t.run_id
LEFT JOIN {SCHEMA}.invocations i ON i.id = t.invocation_id
{_RUN_WORKFLOW}
UNION ALL
SELECT NULL, r.id, r.agent_id, node.workflow_id, i.cause::text,
       coalesce(i.requested_by, r.created_by),
       NULL, r.created_at, NULL, NULL,
       NULL, NULL, NULL, NULL,
       false
FROM {SCHEMA}.agent_runs r
LEFT JOIN {SCHEMA}.invocations i ON i.agent_run_id = r.id
{_RUN_WORKFLOW}
WHERE r.message_count > 0
  AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.turns t WHERE t.agent_run_id = r.id)
""",
        "Each turn of an agent run: its agent, the workflow it ran in, why it ran, who it is"
        " charged to, how it went and its counters. Agent runs from before turns were recorded"
        " are one untracked turn each (tracked false, counters null).",
    ),
    "workflow_runs": (
        f"""
SELECT w.id AS run_id, w.workflow_id, w.workflow_version_id, i.cause::text AS cause,
       coalesce(i.requested_by, w.created_by) AS person_id, w.trigger_id,
       w.status::text AS status, w.created_at, w.started_at, w.finished_at,
       greatest(extract(epoch FROM w.finished_at - w.started_at) - w.waited_seconds, 0)
           ::double precision AS seconds,
       w.waited_seconds
FROM {SCHEMA}.workflow_runs w
LEFT JOIN {SCHEMA}.invocations i ON i.workflow_run_id = w.id
""",
        "Each workflow run: why it ran, who started it, how it went, and its time running"
        " (seconds, once finished) without the time it waited for approvals (waited_seconds).",
    ),
    "workflow_steps": (
        f"""
SELECT st.id AS step_id, st.run_id, w.workflow_id,
       array_to_string(st.path, '/') AS node, st.kind::text AS kind, st.status::text AS status,
       st.started_at, st.finished_at,
       greatest(extract(epoch FROM st.finished_at - st.started_at)
                - coalesce(waits.seconds, 0), 0)::double precision AS seconds,
       coalesce(waits.seconds, 0)::double precision AS waited_seconds
FROM {SCHEMA}.workflow_run_steps st
JOIN {SCHEMA}.workflow_runs w ON w.id = st.run_id
LEFT JOIN LATERAL (
    SELECT sum(extract(epoch FROM
               least(coalesce(a.decided_at, st.finished_at), st.finished_at)
               - greatest(a.created_at, st.started_at))) AS seconds
    FROM {SCHEMA}.schema_change_requests a
    WHERE st.finished_at IS NOT NULL
      AND (a.id = st.approval_id
           OR (st.agent_run_id IS NOT NULL AND a.requested_by_run = st.agent_run_id))
      AND a.created_at < st.finished_at
) waits ON true
""",
        "Each node of each workflow run (node: its path of node keys), how it went, and its time"
        " running (seconds, once finished) without the time it waited for approvals.",
    ),
    "usage": (
        "SELECT * FROM reporting.usage",
        "The usage ledger: each billable unit, its estimated cost in GBP, and what it's"
        " charged to.",
    ),
    "tool_calls": (
        f"""
SELECT c.turn_id, r.agent_id, c.tool, c.calls, t.started_at
FROM {SCHEMA}.turn_tool_calls c
JOIN {SCHEMA}.turns t ON t.id = c.turn_id
JOIN {SCHEMA}.agent_runs r ON r.id = t.agent_run_id
""",
        "How many times each turn called each tool, by name.",
    ),
    "approvals": (
        f"""
SELECT a.id AS approval_id, a.kind, a.status::text AS status, a.approver_role,
       a.created_at, a.decided_at, a.expires_at,
       extract(epoch FROM a.decided_at - a.created_at)::double precision AS wait_seconds
FROM {SCHEMA}.schema_change_requests a
""",
        "Each approval: its kind, its outcome, and how long it waited to be decided"
        " (wait_seconds; expired ones until they expired).",
    ),
    "chats": (
        f"SELECT c.id AS chat_id, c.name, c.created_at, c.archived_at FROM {SCHEMA}.chats c",
        "Each chat, by name.",
    ),
    "chat_messages": (
        f"""
SELECT m.id AS message_id, m.chat_id, m.author_kind::text AS author_kind, m.kind::text AS kind,
       m.author_user_id AS person_id, m.author_agent_id AS agent_id,
       m.author_workflow_id AS workflow_id, jsonb_array_length(m.mentions) AS mentions,
       m.created_at
FROM {SCHEMA}.chat_messages m
""",
        "Each chat message without its text: who wrote it and how many it mentions.",
    ),
    "mentions": (
        f"""
SELECT i.id AS invocation_id, i.chat_id, i.message_id, i.target_kind::text AS target_kind,
       i.agent_id, i.workflow_id, i.status::text AS status, i.requested_by AS person_id,
       i.created_at, i.finished_at
FROM {SCHEMA}.invocations i
WHERE i.cause = 'mention' AND i.depth = 0
""",
        "Each run a mention in a chat started (or refused), and how it went.",
    ),
}


def upgrade() -> None:
    op.create_table(
        "turn_tool_calls",
        sa.Column(
            "turn_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.turns.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tool", sa.String(255), primary_key=True),
        sa.Column("calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=SCHEMA,
        comment="How many times each turn called each tool",
    )
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {INSIGHTS}")
    op.execute(f"COMMENT ON SCHEMA {INSIGHTS} IS 'Metrics-only views for Insights: no content'")
    for name, (body, comment) in VIEWS.items():
        op.execute(f"CREATE VIEW {INSIGHTS}.{name} AS {body}")
        quoted = comment.replace("'", "''")
        op.execute(f"COMMENT ON VIEW {INSIGHTS}.{name} IS '{quoted}'")


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {INSIGHTS} CASCADE")
    op.drop_table("turn_tool_calls", schema=SCHEMA)
