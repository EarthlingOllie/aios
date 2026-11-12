"""connector credentials

`connector_credentials`: each connector's credentials (`aios.connectors`), encrypted with a key
of their own, in the platform database and never in a store. Unique per store and connector;
deleted with the organisation (and its store's row).

Revision ID: 0078
Revises: 0077
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organisation_id",
            sa.Integer(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "data_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("oauth", "github_installation", name="credential_kind"),
            nullable=False,
        ),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_account", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "data_store_id", "connector_id", name="uq_connector_credentials_store_connector"
        ),
    )


def downgrade() -> None:
    op.drop_table("connector_credentials")
    op.execute("DROP TYPE IF EXISTS credential_kind")
