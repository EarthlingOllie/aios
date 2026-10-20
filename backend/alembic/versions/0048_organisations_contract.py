"""organisations contract: owner_id gone

The contract step of organisations. Everything answers to its organisation now (0029 to 0047),
so the columns from before go: `owner_id` on skills, agents, workflows, triggers and chats
(their `organisation_id` is NOT NULL instead), `owner_user_id` on data stores, and the run
history export's flags on users (on the organisation's store since 0035). Their unique names
were made per organisation already (0031, 0043); the indexes on `owner_id` go with it.

This contracts in the same release as the expand, against the expand-then-contract rule, as
0015 did: until the rollout finishes, the previous release's requests fail on the missing
columns. Keeping them a release longer would buy little: that release scopes everything by
`owner_id`, so meanwhile it would show each member only what they made themselves, and none of
what their organisation shares, while this one kept writing a column it never reads. The
platform is in development, as it was then.

Rows made since the data step (0030) and before saves stamped them are stamped first, as it
stamps them (its `backfill` is re-run: it only touches what has no organisation yet), so none
is left without one.

A chat's owner is the role on its owner's member row, which the chat's `owner_id` duplicated.
A chat whose owner has left the organisation lost that row with their membership: it goes to
its user member added first (`hand_over`, as `aios.organisations.leave` now does), and one with
nobody left in it to whoever joins it next. A chat has at most one owner from here.

The downgrade puts the columns back, nullable where they were by 0047, and fills them in as far
as they can be: each row's `owner_id` is its owner (a chat's), else its maker, else its
organisation's creator while a member, else its first member; a store's `owner_user_id` is its
organisation's that way (none for one being deleted), and that user's flags are the store's.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-19 21:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED = ["skills", "agents", "workflows", "triggers", "chats"]
FLAGS = ["export_run_history", "embed_run_history"]
ONE_OWNER = "uq_chat_members_chat_id_owner"


def _backfill(conn: Connection) -> None:
    path = Path(__file__).with_name("0030_personal_organisations.py")
    spec = importlib.util.spec_from_file_location("personal_organisations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.backfill(conn)


def hand_over(conn: Connection) -> None:
    """Make each chat's user member added first its owner, where it has none."""
    conn.execute(
        sa.text(
            "UPDATE chat_members SET role = 'owner' WHERE id IN ("
            " SELECT min(id) FROM chat_members WHERE kind = 'user' AND chat_id NOT IN ("
            "  SELECT chat_id FROM chat_members WHERE role = 'owner')"
            " GROUP BY chat_id)"
        )
    )


def upgrade() -> None:
    conn = op.get_bind()
    _backfill(conn)
    hand_over(conn)
    op.create_index(
        ONE_OWNER,
        "chat_members",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
    )
    for table in OWNED:
        op.alter_column(table, "organisation_id", nullable=False)
        # Its foreign key and index go with it.
        op.drop_column(table, "owner_id")
    op.drop_column("data_stores", "owner_user_id")
    for flag in FLAGS:
        op.drop_column("users", flag)


def _member(organisation_id: str) -> str:
    """SQL for whom a row of the organisation `organisation_id` (an SQL expression) is put back
    as owned by, when nothing on the row says: its creator while a member, else its first."""
    return (
        "(SELECT o.created_by FROM organisations o JOIN organisation_members m"
        "  ON m.organisation_id = o.id AND m.user_id = o.created_by"
        f"  WHERE o.id = {organisation_id}),"
        " (SELECT m.user_id FROM organisation_members m"
        f"  WHERE m.organisation_id = {organisation_id} ORDER BY m.joined_at, m.user_id LIMIT 1)"
    )


# Whom a row of each owned table is put back as owned by, first that applies.
OWNER = {table: f"t.created_by, {_member('t.organisation_id')}" for table in OWNED}
OWNER["chats"] = (
    "(SELECT m.user_id FROM chat_members m WHERE m.chat_id = t.id AND m.role = 'owner'), "
    + OWNER["chats"]
)


def downgrade() -> None:
    for flag in FLAGS:
        op.add_column(
            "users", sa.Column(flag, sa.Boolean(), server_default=sa.text("false"), nullable=False)
        )
    op.add_column("data_stores", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "data_stores_owner_user_id_fkey",
        "data_stores",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("data_stores_owner_user_id_key", "data_stores", ["owner_user_id"])
    # A user is in one organisation, which has one store: no two stores get the same user.
    op.execute(
        f"UPDATE data_stores t SET owner_user_id = coalesce({_member('t.owner_organisation_id')})"
    )
    op.execute(
        "UPDATE users u SET export_run_history = s.export_run_history,"
        " embed_run_history = s.embed_run_history"
        " FROM data_stores s WHERE s.owner_user_id = u.id"
    )
    for table in OWNED:
        op.add_column(table, sa.Column("owner_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} t SET owner_id = coalesce({OWNER[table]})")
        op.alter_column(table, "owner_id", nullable=False)
        op.create_foreign_key(
            f"{table}_owner_id_fkey", table, "users", ["owner_id"], ["id"], ondelete="CASCADE"
        )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
        op.alter_column(table, "organisation_id", nullable=True)
    op.drop_index(ONE_OWNER, table_name="chat_members")
