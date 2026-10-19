"""ownerless private chats: public

A chat whose owner left the organisation goes to its user member added first; one with nobody
left in it goes to whoever joins it next. A private one of those existed for nobody (a private
chat exists only for its members), so nobody could join it, change it or delete it, and its name
stayed taken. Leaving now makes such a chat public (`aios.organisations.hand_over_chats`); this
does the same for those left before: 0048's hand-over, and leaving until now.

Data only; the downgrade leaves them public, as there's no telling them from chats made public.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-19 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def publicise(conn: Connection) -> None:
    """Make each private chat that has no owner and no user member public."""
    conn.execute(
        sa.text(
            "UPDATE chats SET visibility = 'public' WHERE visibility = 'private'"
            " AND id NOT IN (SELECT chat_id FROM chat_members WHERE role = 'owner')"
            " AND id NOT IN (SELECT chat_id FROM chat_members WHERE kind = 'user')"
        )
    )


def upgrade() -> None:
    publicise(op.get_bind())


def downgrade() -> None:
    pass
