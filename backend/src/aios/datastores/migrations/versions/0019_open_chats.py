"""open chats, and Jev's verdict on chat messages

Every chat is open: anyone in the organisation reads it, and posting joins. So a chat's
visibility (`aios.chats.visibility`) and its people's role (`aios.chat_members.role`: owner or
member) go, with their check, the one-owner index and their enum types. Nothing is migrated:
dev data is rebuilt with `make seed`.

`aios.chat_messages` gains its Jev verdict: a person's message is checked as it's posted and
stored `clean`; what agents, workflows and the chat itself post is `derived` (the default), as
are the messages already there.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "aios"


def upgrade() -> None:
    op.drop_index("uq_chat_members_chat_id_owner", "chat_members", schema=SCHEMA)
    op.drop_constraint("ck_chat_members_role", "chat_members", schema=SCHEMA)
    op.drop_column("chat_members", "role", schema=SCHEMA)
    op.drop_column("chats", "visibility", schema=SCHEMA)
    op.execute(f"DROP TYPE {SCHEMA}.chat_member_role")
    op.execute(f"DROP TYPE {SCHEMA}.chat_visibility")
    op.add_column(
        "chat_messages",
        sa.Column(
            "verdict",
            postgresql.ENUM(name="verdict", schema=SCHEMA, create_type=False),
            server_default="derived",
            nullable=False,
            comment="Jev's verdict: clean for a person's message (checked as it's posted),"
            " derived for what agents, workflows and the chat post.",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "verdict", schema=SCHEMA)
    visibility = sa.Enum("public", "private", name="chat_visibility", schema=SCHEMA)
    visibility.create(op.get_bind())
    role = sa.Enum("owner", "member", name="chat_member_role", schema=SCHEMA)
    role.create(op.get_bind())
    op.add_column(
        "chats",
        sa.Column(
            "visibility",
            postgresql.ENUM(name="chat_visibility", schema=SCHEMA, create_type=False),
            server_default="public",
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.alter_column("chats", "visibility", server_default=None, schema=SCHEMA)
    op.add_column(
        "chat_members",
        sa.Column(
            "role",
            postgresql.ENUM(name="chat_member_role", schema=SCHEMA, create_type=False),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    # Every person a member again; each chat's first person its owner.
    op.execute(f"UPDATE {SCHEMA}.chat_members SET role = 'member' WHERE kind = 'user'")
    op.execute(
        f"UPDATE {SCHEMA}.chat_members SET role = 'owner' WHERE id IN"
        f" (SELECT min(id) FROM {SCHEMA}.chat_members WHERE kind = 'user' GROUP BY chat_id)"
    )
    op.create_check_constraint(
        "ck_chat_members_role",
        "chat_members",
        "(kind = 'user') = (role IS NOT NULL)",
        schema=SCHEMA,
    )
    op.create_index(
        "uq_chat_members_chat_id_owner",
        "chat_members",
        ["chat_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("role = 'owner'"),
    )
