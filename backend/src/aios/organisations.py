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
personal one (`ensure_membership`).

**Accepting** a link while signed in (`accept`) moves the user, who is in one organisation at a
time, out of the one they're in. So it's refused (`accept_refusal`) unless that one is **empty**:
personal, with them as its only member, and nothing in it but the defaults (`contents`: no
agents but built-in ones, no workflows, skills, chats, schedules, documents or data tables). A
user who signed up without an invitation can then join a team straight away, and nobody loses
work by joining one. The empty organisation is deleted as they join (`delete_empty`), cascading
to what's left in it, and its store goes `deleting`, detached from any organisation, for the
provisioner's sweep to drop (`aios.datastores.provision.delete_deleting`).

**Leaving** (`leave`) is refused for the last member, whose organisation nobody could see
again, and for the creator while others remain, since nobody else could invite
(`leave_refusal`). Otherwise the user drops out of the organisation's chats, their unread
notifications about its schema change requests go, and they land in a new personal
organisation, so they're never in none. What they made stays in the organisation, and a chat
they owned goes to its member added first (`hand_over_chats`).
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import notifications
from aios.datastores import data_store_for, store_session, tables
from aios.datastores.models import Document
from aios.models import (
    Agent,
    Chat,
    ChatMember,
    ChatMemberKind,
    ChatMemberRole,
    DataStore,
    DataStoreStatus,
    Notification,
    Organisation,
    OrganisationInvite,
    OrganisationMember,
    Skill,
    Trigger,
    User,
    Workflow,
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
    `-2`, `-3`, ... suffix when that's taken (as the data migration named them).

    It reads only the names it could pick: the login, and the login and `-` followed by
    anything (which also has a few it never would, like `sam-team`). The login is escaped
    (`autoescape`), so a `_`, `%` or `\\` in it is itself, not a wildcard or an escape; these
    comparisons are exact and case-sensitive, as the unique constraint on the name is. Missing
    a taken name would make `create_personal` pick it again for ever; reading every name a
    short login's wildcards match would be slow."""
    taken = set(
        db.scalars(
            select(Organisation.name).where(
                (Organisation.name == login)
                | Organisation.name.startswith(f"{login}{SUFFIX}", autoescape=True)
            )
        ).all()
    )
    name, n = login, 2
    while name in taken:
        name, n = f"{login}{SUFFIX}{n}", n + 1
    return name


def create_personal(db: Session, user: User) -> Organisation:
    """A personal organisation for the user, with them as creator and member. Flushed, not
    committed: the caller's transaction commits it with the user.

    The name `personal_name` found free can be taken before it's inserted, by a sign-up, a
    leave or a rename at the same moment. So the insert does nothing on a clash (waiting for
    the other's transaction if it's still open) and the name is picked again, which sees the
    other's now it's committed. A unique violation would instead fail the caller's whole
    transaction (signing in, leaving), where this carries on in it, with no savepoint."""
    while True:
        org_id = db.scalar(
            insert(Organisation)
            .values(name=personal_name(db, user.login), personal=True, created_by=user.id)
            .on_conflict_do_nothing(index_elements=[Organisation.name])
            .returning(Organisation.id)
        )
        if org_id is not None:
            break
    db.add(OrganisationMember(organisation_id=org_id, user_id=user.id))
    db.flush()
    return db.get_one(Organisation, org_id)


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


ONE_AT_A_TIME = "You can be in only one organisation at a time."


# What can be in an organisation, in the order a refusal lists it: each kind, and what one is
# called.
KINDS = {
    "agent": "agent",
    "workflow": "workflow",
    "skill": "skill",
    "chat": "chat",
    "schedule": "schedule",
    "document": "document",
    "table": "data table",
}


def contents(db: Session, org: Organisation) -> list[tuple[str, int]]:
    """What's in the organisation besides its members and the defaults, as (kind, how many) for
    each kind in `KINDS` it has any of: agents but built-in ones (the documents assistant), and
    workflows, skills, chats and schedules, and its store's documents and data tables. Deleted
    agents, workflows and skills don't count: what a member deleted is no work to lose."""
    live = {
        "agent": select(func.count()).where(
            Agent.organisation_id == org.id, Agent.deleted_at.is_(None), Agent.builtin.is_(None)
        ),
        "workflow": select(func.count()).where(
            Workflow.organisation_id == org.id, Workflow.deleted_at.is_(None)
        ),
        "skill": select(func.count()).where(
            Skill.organisation_id == org.id, Skill.deleted_at.is_(None)
        ),
        "chat": select(func.count()).where(Chat.organisation_id == org.id),
        "schedule": select(func.count()).where(Trigger.organisation_id == org.id),
    }
    counts = {kind: db.scalar(query) for kind, query in live.items()}
    counts["document"], counts["table"] = _store_contents(db, org)
    return [(kind, counts[kind]) for kind in KINDS if counts[kind]]


def _store_contents(db: Session, org: Organisation) -> tuple[int, int]:
    """How many documents and data tables the organisation's store has: none until it's ready,
    since nothing can be put in it before. Raises as `store_session` does when it can't be read just
    now (busy, or being upgraded), which the API answers with "try again"."""
    store = db.scalar(select(DataStore).where(DataStore.owner_organisation_id == org.id))
    if store is None or store.status != DataStoreStatus.ready:
        return 0, 0
    with store_session(store.id, "ro") as reading:
        documents = reading.scalar(select(func.count()).select_from(Document))
    return documents, tables.count_tables(store.id)


def _counted(kind: str, n: int) -> str:
    return f"{n} {KINDS[kind]}{'' if n == 1 else 's'}"


@dataclass(frozen=True)
class NotEmpty:
    """Why accepting is refused when the user's organisation has things in it: the reason, and
    what's in it (`contents`), for the invite page to link to where each can be deleted."""

    message: str
    contents: list[tuple[str, int]]


def accept_refusal(db: Session, user: User, invite: OrganisationInvite) -> str | NotEmpty | None:
    """Why the user can't join through the link, or None if they can: they're in no
    organisation, or in an empty one (see the module docstring), which joining deletes.

    Locks the organisation they're in until the caller's transaction ends, so nothing is made
    in it (rows made in it reference it, which takes a lock this one waits for and then blocks)
    and nobody joins it between this check and its deletion. Its store's documents and tables
    are counted, but nothing holds them: one uploaded meanwhile goes with the store."""
    current = membership_of(db, user.id)
    if current is None:
        return None
    if current.id == invite.organisation_id:
        return f"You're already a member of {current.name}."
    db.scalar(select(Organisation.id).where(Organisation.id == current.id).with_for_update())
    members = db.scalar(
        select(func.count()).where(OrganisationMember.organisation_id == current.id)
    )
    if not current.personal or members > 1:
        return f"You're a member of {current.name}. {ONE_AT_A_TIME}"
    if found := contents(db, current):
        listed = [_counted(kind, n) for kind, n in found]
        listing = listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"
        return NotEmpty(
            f"You're a member of {current.name}, which has things in it: {listing}. Accepting "
            f"would delete {current.name}, so delete them first.",
            found,
        )
    return None


def accept(
    db: Session, user: User, invite: OrganisationInvite
) -> tuple[Organisation, uuid.UUID | None]:
    """Join through the link (`accept_refusal` said the user may), deleting the empty
    organisation they leave (`delete_empty`). Returns the organisation joined, and the store
    left to delete, if there is one: queue its deletion (`enqueue_store_deletion`) once this
    commits. Flushed, not committed."""
    current = membership_of(db, user.id)
    store_id = delete_empty(db, current, user) if current is not None else None
    return join(db, user, invite), store_id


def delete_empty(db: Session, org: Organisation, user: User) -> uuid.UUID | None:
    """Delete an empty organisation (`accept_refusal` said it is) as its one member leaves it,
    cascading to its membership, links and the defaults and deleted rows left in it; returns
    its store's id, if it has one. The store goes `deleting` with no organisation, in the same
    transaction (a store with none must be on its way out), and the member's notifications of
    its requests go. Flushed, not committed."""
    store_id = db.scalar(
        select(DataStore.id).where(DataStore.owner_organisation_id == org.id).with_for_update()
    )
    if store_id is not None:
        db.execute(
            update(DataStore)
            .where(DataStore.id == store_id)
            .values(status=DataStoreStatus.deleting, owner_organisation_id=None)
        )
        _forget_requests(db, user, store_id)
    # What pins another's versions first (workflows pin agents', agents pin skills'): one
    # cascading delete of them all can check a pin before it has deleted the pinning row.
    for model in (Workflow, Agent, Skill):
        db.execute(delete(model).where(model.organisation_id == org.id))
    db.execute(delete(Organisation).where(Organisation.id == org.id))
    # The bulk deletes bypass the session: anything it loaded of them is stale.
    db.expire_all()
    return store_id


def enqueue_store_deletion(store_id: uuid.UUID) -> None:
    """Queue the provisioner's sweep for a store just put into `deleting`, so it's dropped now
    rather than at the sweep's next scheduled pass."""
    from aios.worker import delete_data_stores_task

    delete_data_stores_task.delay(str(store_id))


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
    hand_over_chats(db, org)
    store_id = db.scalar(select(DataStore.id).where(DataStore.owner_organisation_id == org.id))
    if store_id is not None:
        _forget_requests(db, user, store_id)
    # The bulk deletes bypass the session: anything it loaded of them (`org.members`) is stale.
    db.expire_all()
    personal = create_personal(db, user)
    # Its store, as signing up makes a new organisation's: provisioned once this commits.
    data_store_for(personal.id, db)
    return personal


def hand_over_chats(db: Session, org: Organisation) -> None:
    """Give each of the organisation's chats that has no owner (its owner left) to its user
    member added first, so a chat is never left that nobody can change or delete. One with
    nobody left in it goes to whoever joins it next (`api.chats.join_chat`). The chats are
    locked first, as joining one with no owner locks it, so the two never both give it one."""
    owned = select(ChatMember.chat_id).where(ChatMember.role == ChatMemberRole.owner)
    ownerless = list(
        db.scalars(
            select(Chat.id)
            .where(Chat.organisation_id == org.id, Chat.id.not_in(owned))
            .with_for_update()
        )
    )
    if not ownerless:
        return
    first = (
        select(func.min(ChatMember.id))
        .where(
            ChatMember.chat_id.in_(ownerless),
            ChatMember.chat_id.not_in(owned),
            ChatMember.kind == ChatMemberKind.user,
        )
        .group_by(ChatMember.chat_id)
    )
    db.execute(update(ChatMember).where(ChatMember.id.in_(first)).values(role=ChatMemberRole.owner))


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
