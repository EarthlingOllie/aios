"""ownerless private chats: deleted

A chat whose owner left the organisation goes to its user member added first; one with nobody
left in it goes to whoever joins it next. A private one of those existed for nobody (a private
chat exists only for its members), so nobody could join it, change it or delete it, and its name
stayed taken. Leaving now deletes such a chat, as its owner deleting it would
(`aios.organisations.hand_over_chats`); this does the same for those left before: 0048's
hand-over, and leaving until now. Their members, messages and invocations go with them, by the
foreign keys' cascades; a run one started stays, as it does when a chat is deleted.

Data only; the downgrade changes nothing, as deleted chats can't be restored.

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


def delete_abandoned(conn: Connection) -> None:
    """Delete each private chat that has no owner and no user member."""
    conn.execute(
        sa.text(
            "DELETE FROM chats WHERE visibility = 'private'"
            " AND id NOT IN (SELECT chat_id FROM chat_members WHERE role = 'owner')"
            " AND id NOT IN (SELECT chat_id FROM chat_members WHERE kind = 'user')"
        )
    )


def upgrade() -> None:
    delete_abandoned(op.get_bind())


def downgrade() -> None:
    pass
