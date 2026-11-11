"""audit outbox

`audit_outbox`: audit entries that platform-side actions (roles, memberships, invitations,
renames, settings, deletion scheduling) write in their own transaction, for `aios.audit.drain`
to deliver to the organisation's store's audit log, each exactly once.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "actor_kind",
            sa.Enum("person", "agent", "system", name="audit_actor_kind"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_agent_id", sa.Integer(), nullable=True),
        sa.Column("actor_run_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.create_index("ix_audit_outbox_organisation_id", "audit_outbox", ["organisation_id"])


def downgrade() -> None:
    op.drop_table("audit_outbox")
    op.execute("DROP TYPE audit_actor_kind")
