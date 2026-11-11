"""audit log, approval rules and the reporting view of the audit log

`aios.audit_log`: the organisation's audit log (`aios.audit`). Append-only: a trigger refuses
updates, deletes and truncation, whoever asks, the store's owner included; the read-write role
is also kept to inserting and reading it, and its details are on the deny list
(`provision.upgrade_store`, which knows the store's role names). `outbox_id` makes delivering a
platform outbox row twice record it once.

`aios.approval_rules`: the least role that may decide each kind of schema change, where an admin
raised it above its default (`aios.approval_rules`).

`reporting.audit_log`: the audit log without its details, beside the run and turn views of the
`reporting` schema (store revision 0013): read-only views for agents, as identifiers, counts,
times and statuses only.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was done.",
        ),
        sa.Column(
            "actor_kind",
            sa.Enum("person", "agent", "system", name="audit_actor_kind", schema=SCHEMA),
            nullable=False,
            comment="person, agent or system.",
        ),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            nullable=True,
            comment="The person who did it (aios.people.id).",
        ),
        sa.Column(
            "actor_agent_id",
            sa.Integer(),
            nullable=True,
            comment="The agent that did it (aios.agents.id).",
        ),
        sa.Column(
            "actor_run_id",
            sa.Integer(),
            nullable=True,
            comment="The agent run it was done in (aios.agent_runs.id).",
        ),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
            comment="What was done, dotted: agent.archived, approval.decided, data.written.",
        ),
        sa.Column(
            "target_kind",
            sa.Text(),
            nullable=True,
            comment="What kind of thing it was done to: agent, member, table, ...",
        ),
        sa.Column("target_id", sa.Text(), nullable=True, comment="Which one: its id, or name."),
        sa.Column(
            "details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("outbox_id", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("outbox_id", name="uq_audit_log_outbox_id"),
        schema=SCHEMA,
        comment="The audit log: who did what, and when. Append-only.",
    )
    op.create_index("ix_audit_log_at_id", "audit_log", ["at", "id"], schema=SCHEMA)
    op.create_index("ix_audit_log_action", "audit_log", ["action"], schema=SCHEMA)
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"], schema=SCHEMA)
    op.execute(
        """
        CREATE FUNCTION aios.audit_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'The audit log is append-only: its entries can''t be changed'
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON aios.audit_log"
        " FOR EACH ROW EXECUTE FUNCTION aios.audit_log_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON aios.audit_log"
        " FOR EACH STATEMENT EXECUTE FUNCTION aios.audit_log_append_only()"
    )

    op.create_table(
        "approval_rules",
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("least_role", sa.Text(), nullable=False, comment="editor or admin."),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "least_role IN ('editor', 'admin')", name="ck_approval_rules_least_role"
        ),
        schema=SCHEMA,
        comment="Who may decide each kind of schema change, where raised from the default",
    )

    # Revision 0013 made the schema (for runs and turns); not if this is ever applied without it.
    op.execute("CREATE SCHEMA IF NOT EXISTS reporting")
    op.execute("COMMENT ON SCHEMA reporting IS 'Read-only views of the platform''s records'")
    op.execute(
        """
        CREATE VIEW reporting.audit_log AS
        SELECT at, actor_kind::text AS actor_kind, actor_user_id, actor_agent_id,
               actor_run_id, action, target_kind, target_id
        FROM aios.audit_log
        """
    )
    op.execute(
        "COMMENT ON VIEW reporting.audit_log IS 'The audit log: who did what (action), to what"
        " (target), and when (at); actor_user_id is aios.people.id, actor_agent_id"
        " aios.agents.id, actor_run_id aios.agent_runs.id'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW reporting.audit_log")
    # The schema stays for revision 0013's views of runs and turns, whose downgrade drops it.
    op.drop_table("approval_rules", schema=SCHEMA)
    op.drop_table("audit_log", schema=SCHEMA)
    op.execute("DROP FUNCTION aios.audit_log_append_only()")
    op.execute("DROP TYPE aios.audit_actor_kind")
