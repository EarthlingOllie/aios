"""Organisations: the groups people join, which own what their members make.

A person is in any number of organisations, or in none: signing up makes nothing, and a person
with none lands on the welcome page, which creates one (`create`, with them as its creator and
only member) or explains invitation links. Membership is `OrganisationMember`, one row per
person and organisation; the request's organisation is resolved in one place,
`aios.auth.current_organisation`, from the organisation prefix in the path
(`/api/o/{organisation_id}/...`, `aios.api.scoping`). Each organisation has a unique slug
(`free_slug`), what links in the browser name it by, made when it's created and never changed by
a rename.

Only the creator renames an organisation and invites into it: a stopgap role until roles exist.
Everything in an organisation is visible to every member.

**Invitation links.** The creator makes a link (`make_invite`) carrying a random token; only the
token's hash is stored, so the link is shown once, when it's made. It lasts a week unless the
creator says otherwise (a month at most) and can be revoked; an expired or revoked link opens for
no one (`open_invite`). Signing up through a link joins its organisation, and only that one.

**Accepting** a link while signed in (`accept`) adds a membership of its organisation, whatever
else the person is in; accepting one into an organisation they're in already adds nothing.

**Leaving** (`leave`) is refused for the last member, who should delete the organisation instead,
and for the creator while others remain, since nobody else could invite (`leave_refusal`).
Otherwise only the membership goes: the person drops out of the organisation's chats (adding
them to one, or their making or joining one, waits for a leave under way and is then refused:
`holds_member`) and their unread notifications about its schema change requests go. Nothing is
made for them: they're left in the organisations they're still in, or in none. What they made
stays in the organisation, and a chat they owned goes to its member added first
(`hand_over_chats`); one with nobody left in it goes to whoever joins it next if it's public, and
is deleted if it's private, as nobody could join it.
"""

import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import notifications
from aios.models import (
    Chat,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    ChatVisibility,
    DataStore,
    Notification,
    Organisation,
    OrganisationInvite,
    OrganisationMember,
    User,
)

# The suffix between a slug and the number that makes it unique: `acme`, `acme-2`, ...
SUFFIX = "-"


def membership(db: Session, user_id: int, organisation_id: int) -> Organisation | None:
    """The organisation, if the user is a member of it: None alike when they aren't and when it
    doesn't exist."""
    return db.scalar(
        select(Organisation)
        .join(OrganisationMember)
        .where(
            OrganisationMember.user_id == user_id,
            OrganisationMember.organisation_id == organisation_id,
        )
    )


def memberships(db: Session, user_id: int) -> list[Organisation]:
    """Every organisation the user is in, the one they joined most recently first."""
    return list(
        db.scalars(
            select(Organisation)
            .join(OrganisationMember)
            .where(OrganisationMember.user_id == user_id)
            .order_by(OrganisationMember.joined_at.desc(), Organisation.id.desc())
        )
    )


# A slug's longest base, before any suffix; and the base of one whose name leaves nothing.
SLUG_MAX = 60
SLUG_FALLBACK = "organisation"


def slugify(name: str) -> str:
    """The base of an organisation's slug: its name in lower-case ASCII words (accents dropped)
    joined by `-`, at most `SLUG_MAX` characters, or `organisation` when nothing's left."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return words[:SLUG_MAX].rstrip("-") or SLUG_FALLBACK


def free_slug(db: Session, name: str) -> str:
    """The slug for a new organisation named `name`: `slugify(name)`, or that with the first
    free `-2`, `-3`, ... suffix when another organisation has it. Reads only the slugs it could
    pick (the base has no `_` or `%`, so `startswith` matches literally)."""
    base = slugify(name)
    taken = set(
        db.scalars(
            select(Organisation.slug).where(
                (Organisation.slug == base) | Organisation.slug.startswith(f"{base}{SUFFIX}")
            )
        ).all()
    )
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}{SUFFIX}{n}", n + 1
    return slug


def create(db: Session, user: User, name: str) -> Organisation:
    """A new organisation named `name`, with `user` as its creator and only member. Flushed, not
    committed: the caller commits it, with its store (`aios.datastores.data_store_for`).

    The slug `free_slug` found can be taken before it's inserted, by another organisation made
    at the same moment, so the insert does nothing on a clash of the slug (waiting for the
    other's transaction if it's still open) and it's picked again, which sees the other's now
    it's committed. A name another organisation has is a unique violation of the name
    (`organisations_name_key`), for the caller to answer."""
    while True:
        org_id = db.scalar(
            insert(Organisation)
            .values(name=name, slug=free_slug(db, name), created_by=user.id)
            .on_conflict_do_nothing(index_elements=["slug"])
            .returning(Organisation.id)
        )
        if org_id is not None:
            break
    db.add(OrganisationMember(organisation_id=org_id, user_id=user.id))
    db.flush()
    return db.get_one(Organisation, org_id)


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


def join(db: Session, user: User, invite: OrganisationInvite) -> Organisation:
    """Make the user a member of the link's organisation, recording the link they came through;
    if they're one already, nothing changes (accepting a link twice adds nothing). Flushed, not
    committed."""
    db.execute(
        insert(OrganisationMember)
        .values(organisation_id=invite.organisation_id, user_id=user.id, invite_id=invite.id)
        .on_conflict_do_nothing(index_elements=["organisation_id", "user_id"])
    )
    db.flush()
    return db.get_one(Organisation, invite.organisation_id)


LAST_MEMBER = (
    "You're its only member, so nobody would be left to see what's in it. Delete the "
    "organisation instead."
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


def holds_member(db: Session, org: Organisation, user_id: int) -> bool:
    """Whether the user is a member of the organisation, kept so until the caller's transaction
    ends: for putting them in one of its chats, which a leave that has already taken them out of
    its chats wouldn't then take them out of. `leave_refusal` locks the organisation before a
    leave takes anyone out, so this locks it too, shared (`FOR KEY SHARE`, as a row referencing
    it does: only leaving, accepting and deleting it wait for that). A leave under way commits
    first and the user is seen gone, or it waits for the caller's commit and takes them out of
    what that put them in. Take it before any other lock, as leaving does, so the two can't
    deadlock, unless the caller has locked the organisation already."""
    db.scalar(
        select(Organisation.id)
        .where(Organisation.id == org.id)
        .with_for_update(read=True, key_share=True)
    )
    member = select(OrganisationMember.user_id).where(
        OrganisationMember.organisation_id == org.id, OrganisationMember.user_id == user_id
    )
    return db.scalar(member) is not None


def leave(db: Session, org: Organisation, user: User) -> None:
    """Take the user out of the organisation (`leave_refusal` said they may): only their
    membership goes, and nothing is made for them. Flushed, not committed."""
    db.execute(
        delete(OrganisationMember).where(
            OrganisationMember.organisation_id == org.id,
            OrganisationMember.user_id == user.id,
        )
    )
    left = db.execute(
        delete(ChatMember)
        .where(
            ChatMember.kind == ChatMemberKind.user,
            ChatMember.user_id == user.id,
            ChatMember.chat_id.in_(select(Chat.id).where(Chat.organisation_id == org.id)),
        )
        .returning(ChatMember.chat_id, ChatMember.role)
    ).all()
    hand_over_chats(db, [chat_id for chat_id, role in left if role == ChatMemberRole.owner])
    store_id = db.scalar(select(DataStore.id).where(DataStore.owner_organisation_id == org.id))
    if store_id is not None:
        _forget_requests(db, user, store_id)
    # The bulk deletes bypass the session: anything it loaded of them (`org.members`) is stale.
    db.expire_all()


def hand_over_chats(db: Session, chat_ids: list[int]) -> None:
    """Give each of these chats, the ones a leaver owned, to its user member added first, so a
    chat is never left that nobody can change or delete. A public one with nobody left in it goes
    to whoever joins it next (`api.chats.join_chat`): the organisation keeps it and its history,
    as it keeps what a leaver made. A private one with nobody left in it is deleted: it exists
    only for its members, so with none it would exist for nobody, nobody could join it, and its
    name would stay taken. Deleted as its owner deleting it is (`api.chats.delete_chat`): a
    statement, its members, messages and invocations going with it in the database, and a run one
    of those started staying, to finish without it.

    Only these, found as the leave deleted the leaver's member rows: no other chat can have lost
    its owner by it (nothing else that changes a chat's members leaves it without one:
    `api.chats.lock_owner`), so a leave holds up nothing in the organisation's other chats. Those
    left without one before leaving handed them over were repaired by revisions 0048, 0049 and
    0051. These are locked before they're read, as every change to a chat's members locks it
    first, so one that read the leaver as its owner either went in before this and is seen by
    it, or waits and sees the owner this gives it, or that the chat is gone."""
    if not chat_ids:
        return
    db.execute(select(Chat.id).where(Chat.id.in_(chat_ids)).with_for_update())
    owned = select(ChatMember.chat_id).where(ChatMember.role == ChatMemberRole.owner)
    first = (
        select(func.min(ChatMember.id))
        .where(
            ChatMember.chat_id.in_(chat_ids),
            ChatMember.chat_id.not_in(owned),
            ChatMember.kind == ChatMemberKind.user,
        )
        .group_by(ChatMember.chat_id)
    )
    db.execute(update(ChatMember).where(ChatMember.id.in_(first)).values(role=ChatMemberRole.owner))
    db.execute(
        delete(Chat).where(
            Chat.id.in_(chat_ids),
            Chat.id.not_in(owned),
            Chat.visibility == ChatVisibility.private,
        )
    )


def _forget_requests(db: Session, user: User, store_id: uuid.UUID) -> None:
    """Delete the user's unread notifications of the store's schema change requests: the store
    is no longer theirs to decide for."""
    db.execute(
        delete(Notification).where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
            Notification.kind == notifications.SCHEMA_CHANGE_REQUEST,
            Notification.ref.contains({"data_store_id": str(store_id)}),
        )
    )
