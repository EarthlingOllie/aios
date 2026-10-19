"""Organisations: the groups users are in, which own what their members make.

Every user is in exactly one organisation at a time. Signing up without an invitation makes a
**personal** one, named after the login (`personal_name`: suffixed when that name is taken),
with the user as its creator and only member; the upgrade did the same for every user from
before (revision 0030). Membership is a table all the same (`OrganisationMember`), held to one
per user by a unique index, so several can come later without a schema change; the request's
organisation is resolved in one place, `aios.auth.current_organisation`, which is where a
request would then say which of its organisations it means. The organisation is never named in
a URL or a header.

Only the creator renames an organisation (and, later, invites into it): a stopgap role until
roles exist. Everything in an organisation is visible to every member.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.models import Organisation, OrganisationMember, User

# The suffix between a login and the number that makes its name unique: `sam`, `sam-2`, `sam-3`.
SUFFIX = "-"


def membership_of(db: Session, user_id: int) -> Organisation | None:
    """The user's one organisation, if they're in one."""
    return db.scalar(
        select(Organisation).join(OrganisationMember).where(OrganisationMember.user_id == user_id)
    )


def personal_name(db: Session, login: str) -> str:
    """The name for a personal organisation: the login, or the login with the first free
    `-2`, `-3`, ... suffix when that's taken (as the data migration named them)."""
    taken = set(
        db.scalars(select(Organisation.name).where(Organisation.name.like(f"{login}%"))).all()
    )
    name, n = login, 2
    while name in taken:
        name, n = f"{login}{SUFFIX}{n}", n + 1
    return name


def create_personal(db: Session, user: User) -> Organisation:
    """A personal organisation for the user, with them as creator and member. Flushed, not
    committed: the caller's transaction commits it with the user."""
    org = Organisation(name=personal_name(db, user.login), personal=True, created_by=user.id)
    org.members.append(OrganisationMember(user_id=user.id))
    db.add(org)
    db.flush()
    return org


def ensure_membership(db: Session, user: User) -> Organisation:
    """The organisation a user signing in lands in: the one they're in, or a new personal one.

    Called on every sign-in, so a user from before organisations whom the upgrade somehow
    missed still gets one. Signing up through an invitation link will join that organisation
    here instead of making a personal one.
    """
    return membership_of(db, user.id) or create_personal(db, user)
