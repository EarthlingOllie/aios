"""The organisation assistant, and the default chat agents

The built-in documents assistant becomes the organisation assistant: its marker is renamed from
`documents` to `assistant`, and the rule of one built-in agent of each kind leaves archived rows
out. The default chat agents (`default_chat_agents`) are the organisation's ordered list of
agents that join every new chat.

No assistant is made here for an organisation that has none: it's made with the organisation
(`aios.assistant`), and dev data is rebuilt with `make seed`.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.drop_index("uq_agents_builtin", "agents", schema=SCHEMA)
    op.execute(f"UPDATE {SCHEMA}.agents SET builtin = 'assistant' WHERE builtin = 'documents'")
    op.create_index(
        "uq_agents_builtin",
        "agents",
        ["builtin"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("builtin IS NOT NULL AND archived_at IS NULL"),
    )
    op.create_table(
        "default_chat_agents",
        sa.Column(
            "agent_id",
            sa.Integer(),
            sa.ForeignKey(f"{SCHEMA}.agents.id"),
            primary_key=True,
            comment="The agent (aios.agents.id).",
        ),
        sa.Column(
            "position", sa.Integer(), nullable=False, comment="Its place in the list, from 0."
        ),
        sa.Column(
            "added_by",
            sa.Integer(),
            nullable=True,
            comment="The person who put it on the list (aios.people.id); null: the app did.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When it was put on the list.",
        ),
        sa.UniqueConstraint("position", name="uq_default_chat_agents_position"),
        schema=SCHEMA,
        comment="Default chat agents: the agents that join every new chat, in order",
    )


def downgrade() -> None:
    op.drop_table("default_chat_agents", schema=SCHEMA)
    op.drop_index("uq_agents_builtin", "agents", schema=SCHEMA)
    op.execute(f"UPDATE {SCHEMA}.agents SET builtin = 'documents' WHERE builtin = 'assistant'")
    op.create_index(
        "uq_agents_builtin",
        "agents",
        ["builtin"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("builtin IS NOT NULL"),
    )
