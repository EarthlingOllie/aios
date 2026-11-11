"""table ownership and shares

`aios.table_owners`: each data table's owning agent, or none (`agent_id` null), with who set it
and when. A table with no row has no owner either: one made before owners, or by hand.

`aios.table_shares`: the agents a table is shared with, who shared it and when. A share hangs
off its table's ownership row, so renaming that row (a drop or a restore renames the table) or
deleting it (a purge) carries the shares with it.

Both name the table as it's named now in `data`: a dropped table's rows go by its dropped name
until it's restored (`aios.datastores.ownership`).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-15 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.create_table(
        "table_owners",
        sa.Column(
            "table_name",
            sa.Text(),
            primary_key=True,
            comment="The data table, as named in the data schema now.",
        ),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agents.id", ondelete="SET NULL"),
            nullable=True,
            comment="The agent that owns it (aios.agents.id); null: no owner.",
        ),
        sa.Column(
            "set_by",
            sa.Integer(),
            nullable=True,
            comment="The person who set it (aios.people.id); null: a schema change did.",
        ),
        sa.Column(
            "set_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was set.",
        ),
        schema=SCHEMA,
        comment="Each data table's owning agent, or none",
    )
    op.create_index("ix_table_owners_agent_id", "table_owners", ["agent_id"], schema=SCHEMA)
    op.create_table(
        "table_shares",
        sa.Column(
            "table_name",
            sa.Text(),
            sa.ForeignKey(
                f"{SCHEMA}.table_owners.table_name", onupdate="CASCADE", ondelete="CASCADE"
            ),
            primary_key=True,
            comment="The data table, as named in the data schema now.",
        ),
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"),
            primary_key=True,
            comment="The agent it's shared with (aios.agents.id).",
        ),
        sa.Column(
            "granted_by",
            sa.Integer(),
            nullable=True,
            comment="The person who shared it (aios.people.id).",
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was shared.",
        ),
        schema=SCHEMA,
        comment="The agents each data table is shared with, who may write it too",
    )
    op.create_index("ix_table_shares_agent_id", "table_shares", ["agent_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("table_shares", schema=SCHEMA)
    op.drop_table("table_owners", schema=SCHEMA)
