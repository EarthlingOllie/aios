"""schedules, chats, invocations and schema change requests

The rest of the organisation's content moves into its store: its schedules (`triggers`), its
chats with their members, messages and invocations (`chats`, `chat_members`, `chat_messages`,
`invocations`, the general table chat invocations became, with mention as their cause), and its
agents' schema change requests (`schema_change_requests`).

Now they're beside the runs, agents and workflows they name, they do so by foreign key: a run
deleted clears what named it (a schedule's last run, an invocation's run, a request's run), a
schedule deleted clears its runs' `trigger_id`, and a workflow step's request is a reference
too. Who made, posted or asked for each stays a platform user id, resolved through the people
mirror. Chats keep their owner and visibility as they were.

Nothing is copied from the platform, whose tables the same release drops: development data is
rebuilt with `make seed`.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def _now() -> sa.TextClause:
    return sa.text("now()")


def _fk(table: str, ondelete: str | None = None, name: str | None = None) -> sa.ForeignKey:
    return sa.ForeignKey(f"{SCHEMA}.{table}.id", ondelete=ondelete, name=name)


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, schema=SCHEMA)


def _existing(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, schema=SCHEMA, create_type=False)


def upgrade() -> None:
    op.create_table(
        "triggers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("kind", _enum("trigger_kind", "schedule"), nullable=False),
        sa.Column("target_kind", _enum("trigger_target_kind", "agent", "workflow"), nullable=False),
        sa.Column("agent_id", sa.Integer(), _fk("agents"), nullable=True),
        sa.Column("workflow_id", sa.Integer(), _fk("workflows"), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("input", sa.Text(), server_default="", nullable=False),
        sa.Column("cron", sa.String(255), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status",
            _enum("trigger_status", "running", "succeeded", "failed"),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_run_id",
            sa.Integer(),
            _fk("agent_runs", "SET NULL", "fk_triggers_last_run_id"),
            nullable=True,
        ),
        sa.Column(
            "last_workflow_run_id",
            sa.Integer(),
            _fk("workflow_runs", "SET NULL", "fk_triggers_last_workflow_run_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_triggers_target",
        ),
        sa.CheckConstraint(
            "kind <> 'schedule' OR (cron IS NOT NULL AND timezone IS NOT NULL)",
            name="ck_triggers_schedule",
        ),
        schema=SCHEMA,
        comment="Schedules: what starts the organisation's agents and workflows by itself",
    )
    op.create_index("ix_triggers_agent_id", "triggers", ["agent_id"], schema=SCHEMA)
    op.create_index("ix_triggers_workflow_id", "triggers", ["workflow_id"], schema=SCHEMA)
    op.create_index(
        "ix_triggers_running",
        "triggers",
        ["heartbeat_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("last_status = 'running'"),
    )
    # The runs a schedule started name it.
    for table in ("agent_runs", "workflow_runs"):
        op.create_foreign_key(
            f"fk_{table}_trigger_id",
            table,
            "triggers",
            ["trigger_id"],
            ["id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
            ondelete="SET NULL",
        )

    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("visibility", _enum("chat_visibility", "public", "private"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        schema=SCHEMA,
        comment="Chats: group conversations between people, agents and workflows",
    )
    op.create_index("uq_chats_name", "chats", ["name"], unique=True, schema=SCHEMA)
    op.create_table(
        "chat_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.Integer(), _fk("chats", "CASCADE"), nullable=False),
        sa.Column("kind", _enum("chat_member_kind", "user", "agent", "workflow"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.Integer(), _fk("agents"), nullable=True),
        sa.Column("workflow_id", sa.Integer(), _fk("workflows"), nullable=True),
        sa.Column("role", _enum("chat_member_role", "owner", "member"), nullable=True),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_read_message_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(kind = 'user' AND user_id IS NOT NULL AND agent_id IS NULL AND workflow_id IS NULL)"
            " OR (kind = 'agent' AND agent_id IS NOT NULL AND user_id IS NULL"
            " AND workflow_id IS NULL)"
            " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND user_id IS NULL"
            " AND agent_id IS NULL)",
            name="ck_chat_members_target",
        ),
        sa.CheckConstraint("(kind = 'user') = (role IS NOT NULL)", name="ck_chat_members_role"),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_chat_members_chat_id_user_id"),
        sa.UniqueConstraint("chat_id", "agent_id", name="uq_chat_members_chat_id_agent_id"),
        sa.UniqueConstraint("chat_id", "workflow_id", name="uq_chat_members_chat_id_workflow_id"),
        schema=SCHEMA,
        comment="Who is in each chat: people, agents and workflows",
    )
    op.create_index(
        "uq_chat_members_chat_id_owner",
        "chat_members",
        ["chat_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("role = 'owner'"),
    )
    for column in ("user_id", "agent_id", "workflow_id"):
        op.create_index(f"ix_chat_members_{column}", "chat_members", [column], schema=SCHEMA)

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.Integer(), _fk("chats", "CASCADE"), nullable=False),
        sa.Column(
            "author_kind",
            _enum("chat_author_kind", "user", "agent", "workflow", "system"),
            nullable=False,
        ),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_agent_id", sa.Integer(), _fk("agents"), nullable=True),
        sa.Column("author_workflow_id", sa.Integer(), _fk("workflows"), nullable=True),
        sa.Column("kind", _enum("chat_message_kind", "text", "run_card"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("invocation_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "(author_kind = 'user' OR author_user_id IS NULL)"
            " AND (author_kind = 'agent' OR author_agent_id IS NULL)"
            " AND (author_kind = 'workflow' OR author_workflow_id IS NULL)",
            name="ck_chat_messages_author",
        ),
        sa.CheckConstraint(
            "kind <> 'run_card' OR invocation_id IS NOT NULL", name="ck_chat_messages_run_card"
        ),
        schema=SCHEMA,
        comment="Each chat's messages, in id order",
    )
    op.create_index(
        "ix_chat_messages_chat_id_id", "chat_messages", ["chat_id", "id"], schema=SCHEMA
    )
    for column in ("invocation_id", "author_user_id"):
        op.create_index(f"ix_chat_messages_{column}", "chat_messages", [column], schema=SCHEMA)

    op.create_table(
        "invocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cause",
            _enum("invocation_cause", "mention", "schedule", "manual"),
            server_default="mention",
            nullable=False,
        ),
        sa.Column("chat_id", sa.Integer(), _fk("chats", "CASCADE"), nullable=True),
        sa.Column("message_id", sa.Integer(), _fk("chat_messages", "CASCADE"), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("target_kind", _existing("trigger_target_kind"), nullable=False),
        sa.Column("agent_id", sa.Integer(), _fk("agents"), nullable=True),
        sa.Column("workflow_id", sa.Integer(), _fk("workflows"), nullable=True),
        sa.Column(
            "status",
            _enum("invocation_status", "queued", "briefing", "running", "succeeded", "failed"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("brief", sa.Text(), nullable=True),
        sa.Column("agent_run_id", sa.Integer(), _fk("agent_runs", "SET NULL"), nullable=True),
        sa.Column("workflow_run_id", sa.Integer(), _fk("workflow_runs", "SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(target_kind = 'agent' AND agent_id IS NOT NULL AND workflow_id IS NULL)"
            " OR (target_kind = 'workflow' AND workflow_id IS NOT NULL AND agent_id IS NULL)",
            name="ck_invocations_target",
        ),
        sa.CheckConstraint(
            "cause <> 'mention' OR (chat_id IS NOT NULL AND message_id IS NOT NULL)",
            name="ck_invocations_mention",
        ),
        schema=SCHEMA,
        comment="Why each run ran: so far, a mention in a chat",
    )
    for column in ("chat_id", "message_id", "agent_run_id", "workflow_run_id"):
        op.create_index(f"ix_invocations_{column}", "invocations", [column], schema=SCHEMA)
    op.create_index(
        "ix_invocations_unfinished",
        "invocations",
        ["chat_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('queued', 'briefing', 'running')"),
    )
    op.create_foreign_key(
        "fk_chat_messages_invocation_id",
        "chat_messages",
        "invocations",
        ["invocation_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="CASCADE",
    )

    op.create_table(
        "schema_change_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("ops", postgresql.JSONB(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by_run", sa.Integer(), _fk("agent_runs", "SET NULL"), nullable=True),
        sa.Column("tool_call_id", sa.String(255), nullable=True),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "schema_change_status",
                "pending",
                "approved",
                "rejected",
                "expired",
                "applied",
                "failed",
                "stale",
                "cancelled",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint(
            "requested_by_run", "tool_call_id", name="uq_schema_change_requests_run_tool_call"
        ),
        schema=SCHEMA,
        comment="Agents' requests to change the data schema, and what was decided",
    )
    op.create_index(
        "ix_schema_change_requests_requested_by_run",
        "schema_change_requests",
        ["requested_by_run"],
        schema=SCHEMA,
    )
    # A waiting workflow step's request.
    op.create_foreign_key(
        "fk_workflow_run_steps_waiting_on_request_id",
        "workflow_run_steps",
        "schema_change_requests",
        ["waiting_on_request_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflow_run_steps_waiting_on_request_id",
        "workflow_run_steps",
        schema=SCHEMA,
        type_="foreignkey",
    )
    for table in ("agent_runs", "workflow_runs"):
        op.drop_constraint(f"fk_{table}_trigger_id", table, schema=SCHEMA, type_="foreignkey")
    op.drop_constraint(
        "fk_chat_messages_invocation_id", "chat_messages", schema=SCHEMA, type_="foreignkey"
    )
    for table in (
        "schema_change_requests",
        "invocations",
        "chat_messages",
        "chat_members",
        "chats",
        "triggers",
    ):
        op.drop_table(table, schema=SCHEMA)
    for enum in (
        "schema_change_status",
        "invocation_status",
        "invocation_cause",
        "chat_message_kind",
        "chat_author_kind",
        "chat_member_role",
        "chat_member_kind",
        "chat_visibility",
        "trigger_status",
        "trigger_target_kind",
        "trigger_kind",
    ):
        op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.{enum}")
