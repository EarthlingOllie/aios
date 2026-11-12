"""Approval rules: the least role that may decide each kind of schema change, in the
organisation's store (`aios.approval_rules`, `aios.datastores.models.ApprovalRule`).

| Kind | Default |
| --- | --- |
| create table, add column, create index | editor |
| drop table, drop column, restore, purge, change ownership | admin |

A kind with no row has its default. Admins may raise a kind's role, never lower it below the
default (`Lowered`). A request needs the highest role among its operations (`needed`): one that
adds a column and drops another needs an admin. Whom a new request notifies, who may decide one,
and what the Data page shows each member all read these (`aios.datastores.resolve.approvers`,
`aios.api.approvals`).
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import audit
from aios.datastores.models import ApprovalRule
from aios.models import Role

# Each kind's default, in the order they're listed.
DEFAULTS: dict[str, Role] = {
    "create_table": Role.editor,
    "add_column": Role.editor,
    "create_index": Role.editor,
    "drop_table": Role.admin,
    "drop_column": Role.admin,
    "restore": Role.admin,
    "purge": Role.admin,
    "change_ownership": Role.admin,
}

LABELS: dict[str, str] = {
    "create_table": "Create a table",
    "add_column": "Add a column",
    "create_index": "Create an index",
    "drop_table": "Drop a table",
    "drop_column": "Drop a column",
    "restore": "Restore a dropped table or column",
    "purge": "Purge a dropped table or column",
    "change_ownership": "Change a table's owner",
}

# From the least that may do anything to the most.
RANK: dict[Role, int] = {Role.participant: 0, Role.editor: 1, Role.admin: 2}


def at_least(role: Role, least: Role) -> bool:
    """Whether `role` is `least` or above it."""
    return RANK[role] >= RANK[least]


def highest(roles: Iterable[Role]) -> Role:
    return max(roles, key=RANK.__getitem__, default=Role.editor)


class Lowered(ValueError):
    """A rule set below its kind's default."""


def current(store: Session) -> dict[str, Role]:
    """Each kind's least role in the store: its row's, else its default."""
    raised = dict(store.execute(select(ApprovalRule.kind, ApprovalRule.least_role)).all())
    return {kind: Role(raised.get(kind, default)) for kind, default in DEFAULTS.items()}


def kinds_of(ops: Iterable[dict[str, Any]]) -> set[str]:
    """The kinds of change a request's operations (as stored) are."""
    return {op["op"] for op in ops}


def needed(rules: dict[str, Role], ops: Iterable[dict[str, Any]]) -> Role:
    """The role a request with these operations needs: the highest of theirs (an unknown kind's
    is an admin's)."""
    return highest(rules.get(kind, Role.admin) for kind in kinds_of(ops))


def needed_for(store: Session, ops: Iterable[dict[str, Any]]) -> Role:
    return needed(current(store), ops)


def set_rule(store: Session, kind: str, role: Role, user_id: int) -> Role:
    """Set the kind's least role, as the admin `user_id`, and audit it, in the caller's store
    transaction; answers what it was. Raises KeyError for an unknown kind, `Lowered` below its
    default."""
    default = DEFAULTS[kind]
    if not at_least(role, default):
        raise Lowered(
            f"{LABELS[kind]} needs at least {a(default)} to approve: a rule can be raised, but"
            " not lowered below its default."
        )
    before = current(store)[kind]
    store.execute(
        insert(ApprovalRule)
        .values(kind=kind, least_role=role.value, updated_by=user_id)
        .on_conflict_do_update(
            index_elements=[ApprovalRule.kind],
            set_={"least_role": role.value, "updated_by": user_id, "updated_at": func.now()},
        )
    )
    if before != role:
        audit.record(
            store,
            audit.person(user_id),
            "approval_rule.changed",
            "approval_rule",
            kind,
            {"from": before.value, "to": role.value},
        )
    return before


def a(role: Role) -> str:
    return {Role.admin: "an admin", Role.editor: "an editor", Role.participant: "a participant"}[
        role
    ]
