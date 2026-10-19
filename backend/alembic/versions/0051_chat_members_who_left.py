"""chat members who left: out

A user who left the organisation as they were added to one of its chats, made one or joined one
could be left in it: their leave had already taken them out of its chats, and the add, the chat
or the join committed after. Their chat would then exist for nobody who could manage it, if they
owned it or were later handed it as its member added first. Adding, making and joining now wait
for a leave under way (`aios.organisations.holds_member`); this repairs what it left before.

Each chat's user members who aren't members of its organisation go (0043's `drop_outsiders`),
what they posted staying, as it does when anyone leaves. A chat that loses its owner that way
goes to its user member added first (0048's `hand_over`), and a private one with nobody left in
it is deleted (0049's `delete_abandoned`), as leaving does; a public one goes to whoever joins it.

Data only; the downgrade changes nothing, as there's no putting them back, and deleted chats
can't be restored.

Revision ID: 0051
Revises: 0049
Create Date: 2026-10-19 23:00:00
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0051"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _revision(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    conn = op.get_bind()
    _revision("0043_chat_names_unique_per_organisation").drop_outsiders(conn)
    _revision("0048_organisations_contract").hand_over(conn)
    _revision("0049_ownerless_private_chats").delete_abandoned(conn)


def downgrade() -> None:
    pass
