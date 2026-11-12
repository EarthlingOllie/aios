"""Roles and permissions: what each role may do in an organisation, from one table.

Every member has one role in each organisation they're in (`aios.models.Role`): admin, editor or
participant. `TABLE` says which roles may take each action, following the spec's permission
table; `allow(action)` is the one check every organisation route declares (as a route
dependency, so it runs before anything else the route does), and it answers 403 naming the
action for a role that may not (`refusal`). A route under the organisation prefix that doesn't
declare exactly one stops the app starting (`aios.api.scoping`), so none is left unguarded.

What a route does beyond its action is checked in it: the chat rules (`aios.api.chats.rules`),
a mention's guards (`aios.guards`), and an organisation keeping an admin
(`aios.organisations.change_role`, `leave_refusal`).
"""

import enum
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from aios.auth import CurrentOrganisation
from aios.models import Role


class Action(enum.StrEnum):
    """What a member does, as a refusal names it ("Participants can't ...")."""

    read = "read and search what's in the organisation"
    post = "chat, mention and start runs"
    build = "create, edit, archive or unarchive agents, skills, workflows and schedules"
    documents = "upload or delete documents"
    decide = "approve or reject schema changes and workflow approvals"
    restore = "restore dropped tables and columns"
    tables = "share, unshare or transfer data tables"
    sources = "add, change, pause or remove sources"
    run_organisation = (
        "change the organisation's roles, invitations, name or settings, or delete it"
    )
    leave = "leave the organisation"
    audit = "read the audit log"
    usage = "read usage totals"
    release = "see flagged content or release it"
    archive_chats = "archive or unarchive chats"


EVERYONE = frozenset(Role)
BUILDERS = frozenset({Role.admin, Role.editor})
ADMINS = frozenset({Role.admin})

# Which roles may take each action.
TABLE: dict[Action, frozenset[Role]] = {
    Action.read: EVERYONE,
    Action.post: EVERYONE,
    Action.build: BUILDERS,
    Action.documents: BUILDERS,
    # And at least the role the request needs, by the approval rules (`aios.approval_rules`).
    Action.decide: BUILDERS,
    Action.restore: ADMINS,
    # And at least the role the approval rules ask to change an owner (`aios.approval_rules`).
    Action.tables: BUILDERS,
    Action.sources: ADMINS,
    Action.run_organisation: ADMINS,
    Action.leave: EVERYONE,
    Action.audit: ADMINS,
    Action.usage: ADMINS,
    Action.release: ADMINS,
    # Their only power over chats beyond anyone's (`aios.api.chats.rules`).
    Action.archive_chats: ADMINS,
}

_A = {Role.admin: "an admin", Role.editor: "an editor", Role.participant: "a participant"}
_PLURAL = {Role.admin: "Admins", Role.editor: "Editors", Role.participant: "Participants"}


def can(role: Role, action: Action) -> bool:
    """Whether a member with `role` may take `action`."""
    return role in TABLE[action]


def refusal(role: Role, action: Action) -> str:
    """Why a member with `role` can't take `action`: the action, and whom to ask."""
    # The least role that may first: "an editor or an admin".
    who = " or ".join(_A[r] for r in reversed(Role) if r in TABLE[action])
    return f"{_PLURAL[role]} can't {action.value}. Ask {who}."


class PermissionRefused(HTTPException):
    def __init__(self, role: Role, action: Action):
        super().__init__(status.HTTP_403_FORBIDDEN, refusal(role, action))
        self.action = action


def check(role: Role, action: Action) -> None:
    """Refuse (403, naming the action) unless `role` may take `action`."""
    if not can(role, action):
        raise PermissionRefused(role, action)


def current_role(request: Request, org: CurrentOrganisation) -> Role:
    """The signed-in member's role in the request's organisation, as `current_organisation`
    found it with their membership."""
    return request.state.organisation_role


CurrentRole = Annotated[Role, Depends(current_role)]


class Permit:
    """The route dependency `allow` makes: the member's role in the request's organisation may
    take `action`."""

    def __init__(self, action: Action):
        self.action = action

    def __call__(self, role: CurrentRole) -> None:
        check(role, self.action)

    def __repr__(self) -> str:
        return f"Permit({self.action.name})"


def allow(action: Action) -> Any:
    """The permission check for a route of an organisation, as one of its `dependencies`:
    `@router.post(..., dependencies=[allow(Action.build)])`."""
    return Depends(Permit(action))


def permit_of(call: Any) -> Permit | None:
    """The `Permit` a dependency is, if it's one `allow` made."""
    return call if isinstance(call, Permit) else None
