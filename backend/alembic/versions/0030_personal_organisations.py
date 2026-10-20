"""personal organisations

The data step of organisations: every user made before them gets a personal organisation named
after their login (suffixed `-2`, `-3`, ... when that name is taken, in the order they signed
up), with them as its creator and only member, and everything they own (skills, agents,
workflows, triggers, chats) is stamped with that organisation and with them as `created_by`.
Who last changed or deleted a row isn't known, so `updated_by` and `deleted_by` stay null, as
does `created_by` on runs from before.

`backfill` is the step itself, over a connection, so it can be run (and tested) on its own; it
skips users already in an organisation, so running it again changes nothing.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-19 09:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED = ["skills", "agents", "workflows", "triggers", "chats"]


def backfill(conn: Connection) -> None:
    taken = {name for (name,) in conn.execute(sa.text("SELECT name FROM organisations"))}
    users = conn.execute(
        sa.text(
            "SELECT u.id, u.login FROM users u"
            " LEFT JOIN organisation_members m ON m.user_id = u.id"
            " WHERE m.user_id IS NULL ORDER BY u.id"
        )
    ).all()
    for user_id, login in users:
        name, n = login, 2
        while name in taken:
            name, n = f"{login}-{n}", n + 1
        taken.add(name)
        organisation_id = conn.execute(
            sa.text(
                "INSERT INTO organisations (name, personal, created_by)"
                " VALUES (:name, true, :user_id) RETURNING id"
            ),
            {"name": name, "user_id": user_id},
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO organisation_members (organisation_id, user_id)"
                " VALUES (:organisation_id, :user_id)"
            ),
            {"organisation_id": organisation_id, "user_id": user_id},
        )
    for table in OWNED:
        conn.execute(
            sa.text(
                f"UPDATE {table} t SET organisation_id = m.organisation_id, created_by = t.owner_id"
                " FROM organisation_members m"
                " WHERE m.user_id = t.owner_id AND t.organisation_id IS NULL"
            )
        )


def upgrade() -> None:
    backfill(op.get_bind())


def downgrade() -> None:
    # Back to no organisations. The owned rows are unstamped first: deleting an organisation
    # would otherwise cascade to them.
    for table in OWNED:
        op.execute(f"UPDATE {table} SET organisation_id = NULL, created_by = NULL")
    op.execute("DELETE FROM organisations")
