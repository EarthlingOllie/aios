"""connectors

`aios.connectors`: connections to external systems (`aios.connectors`), one per provider, with
the scope an admin picked. Their credentials are in the platform database, never here.

`aios.sources` gains `connector_id`: the connector a source of kind `connector` syncs (one
source per connector; removed with it).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "provider",
            sa.String(64),
            nullable=False,
            comment="slack, google_drive, github or test.",
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="What it syncs: the channels, folders or repositories picked, each as"
            " {id, name, restricted}.",
        ),
        sa.Column(
            "status",
            sa.Enum("active", "needs_reauth", "removing", name="connector_status", schema=SCHEMA),
            server_default="active",
            nullable=False,
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("provider", name="uq_connectors_provider"),
        schema=SCHEMA,
        comment="Connections to external systems, one per provider",
    )
    op.add_column(
        "sources",
        sa.Column(
            "connector_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.connectors.id", ondelete="CASCADE"),
            nullable=True,
            comment="The connector it syncs (kind connector, made from one).",
        ),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "sources_connector_id_key", "sources", ["connector_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint("sources_connector_id_key", "sources", schema=SCHEMA)
    op.drop_column("sources", "connector_id", schema=SCHEMA)
    op.drop_table("connectors", schema=SCHEMA)
    op.execute(f"DROP TYPE IF EXISTS {SCHEMA}.connector_status")
