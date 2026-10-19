"""Organisations: the groups users are in, which own what their members make.

Every user is in exactly one organisation at a time. Signing up without an invitation makes a
**personal** one, named after the login (`personal_name`: suffixed when that name is taken),
with the user as its creator and only member; the upgrade did the same for every user from
before (revision 0030). Membership is a table all the same (`OrganisationMember`), held to one
per user by a unique index, so several can come later without a schema change; the request's
organisation is resolved in one place, `aios.auth.current_organisation`, which is where a
request would then say which of its organisations it means. The organisation is never named in
a URL or a header.

Only the creator renames an organisation and invites into it: a stopgap role until roles exist.
Everything in an organisation is visible to every member.

**Invitation links.** The creator makes a link (`make_invite`) carrying a random token; only the
token's hash is stored, so the link is shown once, when it's made. It lasts a week unless the
creator says otherwise (a month at most) and can be revoked; an expired or revoked link opens for
no one (`open_invite`). Signing up through a link joins its organisation instead of making a
personal one (`ensure_membership`); a signed-in user accepts it (`join`) only if they're in no
organisation (`accept_refusal`), since a user is in one at a time.

**Leaving** (`leave`) is refused for the last member, whose organisation nobody could see
again, and for the creator while others remain, since nobody else could invite
(`leave_refusal`). Otherwise the user drops out of the organisation's chats, their unread
notifications about its schema change requests go, and they land in a new personal
organisation, so they're never in none. What they made stays in the organisation.
"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aios import notifications
from aios.datastores import data_store_for
from aios.models import (
    Chat,
    ChatMember,
    ChatMemberKind,
    DataStore,
    Notification,
    Organisation,
    OrganisationInvite,
    OrganisationMember,
    User,
)

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


def ensure_membership(
    db: Session, user: User, invite: OrganisationInvite | None = None
) -> Organisation:
    """The organisation a user signing in lands in: the one they're in; else the one the open
    invitation link they came through is for (`open_invite`); else a new personal one.

    Called on every sign-in, so a user from before organisations whom the upgrade somehow
    missed still gets one. A link doesn't move a user who's in an organisation already: they
    accept it from its page, where the refusal says why.
    """
    org = membership_of(db, user.id)
    if org is not None:
        return org
    if invite is not None:
        return join(db, user, invite)
    return create_personal(db, user)


# How long a link lasts unless its creator says otherwise, and the longest it may.
INVITE_DAYS = 7
INVITE_DAYS_MAX = 30


def hash_token(token: str) -> str:
    """What's stored of a link's token: enough to find the link again from it, not to make it."""
    return hashlib.sha256(token.encode()).hexdigest()


def make_invite(
    db: Session, org: Organisation, user: User, days: int = INVITE_DAYS
) -> tuple[OrganisationInvite, str]:
    """A new invitation link into the organisation, made by `user` (its creator), and its
    token: the only time the token exists outside the link. Flushed, not committed."""
    token = secrets.token_urlsafe(32)
    invite = OrganisationInvite(
        organisation_id=org.id,
        token_hash=hash_token(token),
        created_by=user.id,
        expires_at=func.now() + timedelta(days=days),
    )
    db.add(invite)
    db.flush()
    db.refresh(invite)
    return invite, token


def is_open():
    """The condition that a link still opens: not revoked and not expired."""
    return OrganisationInvite.revoked_at.is_(None) & (OrganisationInvite.expires_at > func.now())


def open_invite(db: Session, token: str, lock: bool = False) -> OrganisationInvite | None:
    """The open link the token is for, if any (an expired or revoked one is none). `lock` holds
    it against being revoked until the caller's transaction ends, for joining through it."""
    query = select(OrganisationInvite).where(
        OrganisationInvite.token_hash == hash_token(token), is_open()
    )
    return db.scalar(query.with_for_update(read=True) if lock else query)


def open_invites(db: Session, org: Organisation) -> list[OrganisationInvite]:
    """The organisation's links that still open, oldest first."""
    return list(
        db.scalars(
            select(OrganisationInvite)
            .where(OrganisationInvite.organisation_id == org.id, is_open())
            .order_by(OrganisationInvite.created_at, OrganisationInvite.id)
        )
    )


def accept_refusal(db: Session, user: User, invite: OrganisationInvite) -> str | None:
    """Why the user can't join through the link, or None if they can: they're in an
    organisation already, and a user is in one at a time."""
    current = membership_of(db, user.id)
    if current is None:
        return None
    if current.id == invite.organisation_id:
        return f"You're already a member of {current.name}."
    return f"You're a member of {current.name}. {ONE_AT_A_TIME}"


ONE_AT_A_TIME = "You can be in only one organisation at a time."


def join(db: Session, user: User, invite: OrganisationInvite) -> Organisation:
    """Make the user (in no organisation) a member of the link's organisation, recording the
    link they came through. Flushed, not committed."""
    db.add(
        OrganisationMember(
            organisation_id=invite.organisation_id, user_id=user.id, invite_id=invite.id
        )
    )
    db.flush()
    return db.get_one(Organisation, invite.organisation_id)


LAST_MEMBER = (
    "You're its only member, so nobody would be left to see what's in it: an organisation "
    "can't be left empty."
)
CREATOR_WITH_OTHERS = (
    "You created this organisation, and only its creator can invite people into it, so you "
    "can't leave while others are still members."
)


def leave_refusal(db: Session, org: Organisation, user: User) -> str | None:
    """Why the user can't leave the organisation, or None if they can. Locks it, so two last
    members can't both leave it at once."""
    db.scalar(select(Organisation.id).where(Organisation.id == org.id).with_for_update())
    members = db.scalar(select(func.count()).where(OrganisationMember.organisation_id == org.id))
    if members <= 1:
        return LAST_MEMBER
    if org.created_by == user.id:
        return CREATOR_WITH_OTHERS
    return None


def leave(db: Session, org: Organisation, user: User) -> Organisation:
    """Take the user out of the organisation (`leave_refusal` said they may) and into a new
    personal one, which gets a data store as a new user's does. Flushed, not committed."""
    db.execute(
        delete(OrganisationMember).where(
            OrganisationMember.organisation_id == org.id,
            OrganisationMember.user_id == user.id,
        )
    )
    db.execute(
        delete(ChatMember).where(
            ChatMember.kind == ChatMemberKind.user,
            ChatMember.user_id == user.id,
            ChatMember.chat_id.in_(select(Chat.id).where(Chat.organisation_id == org.id)),
        )
    )
    store_id = data_store_of(db, org)
    if store_id is not None:
        db.execute(
            delete(Notification).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
                Notification.kind == notifications.SCHEMA_CHANGE_REQUEST,
                Notification.ref.contains({"data_store_id": str(store_id)}),
            )
        )
    db.expire_all()
    personal = create_personal(db, user)
    give_data_store(db, personal, user)
    return personal


# Where an organisation's data store is found and made. Stores still hang off a user
# (`DataStore.owner_user_id`), so these are the two places that say which.


def data_store_of(db: Session, org: Organisation) -> uuid.UUID | None:
    """The organisation's data store, if it has one: its creator's."""
    return db.scalar(select(DataStore.id).where(DataStore.owner_user_id == org.created_by))


def give_data_store(db: Session, org: Organisation, user: User) -> None:
    """Give a new organisation, made for `user`, its data store, as signing up gives a new
    user one: in the caller's transaction, provisioned once it commits."""
    data_store_for(user.id, db)
