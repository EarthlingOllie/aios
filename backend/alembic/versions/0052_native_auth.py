"""native auth

Users sign up and in with an email and password instead of GitHub: `users` loses its GitHub
columns (`github_id`, `login`, `name`, `avatar_url`) and gains a first and last name, a unique
(normalised) email, a password hash and a session version.

The GitHub-era users can't be carried over (they have no password, and maybe no email), so
they're deleted, with everything their organisations own: with every user gone, every
organisation is left with no members, and nobody could ever see it again. Each organisation's
data store goes `deleting`, with no organisation, as deleting an empty organisation does
(`aios.organisations.delete_empty`), for the provisioner's sweep to drop its database, roles
and files (`aios.datastores.provision.delete_deleting`); then the organisations go, cascading
to what's in them (what pins another's versions first: workflows pin agents', agents pin
skills'), and the users, cascading to their memberships, chat memberships and notifications.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-20 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _delete_everyone() -> None:
    op.execute(
        "UPDATE data_stores SET status = 'deleting', owner_organisation_id = NULL"
        " WHERE status NOT IN ('deleting', 'deleted')"
    )
    for table in ("workflows", "agents", "skills", "organisations", "users"):
        op.execute(f"DELETE FROM {table}")


def upgrade() -> None:
    _delete_everyone()
    op.drop_constraint("users_github_id_key", "users", type_="unique")
    for column in ("github_id", "login", "name", "avatar_url", "email"):
        op.drop_column("users", column)
    op.add_column("users", sa.Column("first_name", sa.String(length=100), nullable=False))
    op.add_column("users", sa.Column("last_name", sa.String(length=100), nullable=False))
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=False))
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=False))
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_unique_constraint("users_email_key", "users", ["email"])


def downgrade() -> None:
    # Native users can't become GitHub ones either.
    _delete_everyone()
    op.drop_constraint("users_email_key", "users", type_="unique")
    for column in ("first_name", "last_name", "email", "password_hash", "session_version"):
        op.drop_column("users", column)
    op.add_column("users", sa.Column("github_id", sa.BigInteger(), nullable=False))
    op.add_column("users", sa.Column("login", sa.String(length=255), nullable=False))
    op.add_column("users", sa.Column("name", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))
    op.create_unique_constraint("users_github_id_key", "users", ["github_id"])
