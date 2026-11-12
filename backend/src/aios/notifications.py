"""In-app notifications: rows in `notifications`, each for one person, from one of their
organisations, of a kind, with a reference (jsonb) to what it's about there. A reference never
holds content: what a notification says is read from the organisation's store when it's listed
(`describe`), and one whose reference no longer resolves is dropped, and marked read.

**Kinds**, each with what its reference names, who gets it, and what marks it read:

- `mention`: the message (`chat_id`, `message_id`); the people a person's message mentions,
  never its poster, or an agent's post mentions (`aios.tools.post_message`); reading the chat
  past it.
- `reply`: the result posted (`chat_id`, `message_id`); whoever mentioned the agent or
  workflow, unless they're watching the chat's stream in a visible tab; reading past it.
- `run_failed`: the failure posted, as a reply's; for a run started by hand, the run
  (`agent_id` or `workflow_id`, and `run_id`); whoever caused it; reading past it, or opening it.
- `approval_needed`: the approval (`approval_id`: a schema change, or a workflow approval
  step, `aios.approvals`); whoever may decide it, and only them; its being decided, cancelled
  or expired, for everyone.
- `released_document_changed`: the document (`document_id`); the admin who released it.
- `released_item_changed`: a released synced item that changed at its source
  (`synced_item_id`); every admin.
- `source_attention`: a source that became failing or needs reconnecting (`source_id`); every
  admin, once (on the change, not on each failed sync).
- `organisation_deletion_scheduled` and `_cancelled`: the organisation itself (`{}`); every
  member.
- `budget_warning` and `budget_reached`: the month (`month`, `YYYY-MM`) the organisation's
  spending passed the warning threshold of its budget, or reached it (`aios.budgets`); every
  admin, once a month each.

At most one is unread per person, organisation, kind and reference: a repeat adds nothing
(`notify`). **Mark all read** clears a person's (`mark_all_read`); leaving an organisation
deletes theirs from it (`forget_organisation`); read ones older than
`notification_retention_days` are pruned daily (`prune`).

**Live.** Whenever a person's notifications change (new, or read), once that commits their
stream (`aios.streams.person_channel`) gets a `notification` event: the change and their unread
count. The bell reads its list again on it.

What the store side finds (a message posted, a run ending) is noted on the store's session
(`after_store_commit`) and notified once that commits, in a platform transaction of its own.
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, cast, delete, func, select, update
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.db import SessionLocal
from aios.models import DataStore, Notification, OrganisationMember, Role

log = logging.getLogger(__name__)

MENTION = "mention"
REPLY = "reply"
RUN_FAILED = "run_failed"
APPROVAL_NEEDED = "approval_needed"
RELEASED_DOCUMENT_CHANGED = "released_document_changed"
RELEASED_ITEM_CHANGED = "released_item_changed"
SOURCE_ATTENTION = "source_attention"
ORGANISATION_DELETION_SCHEDULED = "organisation_deletion_scheduled"
ORGANISATION_DELETION_CANCELLED = "organisation_deletion_cancelled"
BUDGET_WARNING = "budget_warning"
BUDGET_REACHED = "budget_reached"
# What reading a chat past a message clears.
CHAT_KINDS = (MENTION, REPLY, RUN_FAILED)

# A `notification` event's change.
NEW = "new"
READ = "read"


def chat_ref(chat_id: int, message_id: int) -> dict[str, int]:
    return {"chat_id": chat_id, "message_id": message_id}


def approval_ref(approval_id: uuid.UUID | str) -> dict[str, str]:
    return {"approval_id": str(approval_id)}


# --- Writing them --------------------------------------------------------------------------------


def notify(
    db: Session, user_ids: Iterable[int], organisation_id: int, kind: str, ref: dict[str, Any]
) -> None:
    """Add a notification of `kind` about `ref`, from the organisation, for each of the users
    still in it, in the caller's transaction (so it commits, or doesn't, with what it's about).
    Nothing for one who has an unread one about it already."""
    user_ids = set(user_ids)
    if not user_ids:
        return
    members = db.scalars(
        select(OrganisationMember.user_id).where(
            OrganisationMember.organisation_id == organisation_id,
            OrganisationMember.user_id.in_(user_ids),
        )
    ).all()
    if not members:
        return
    added = db.scalars(
        insert(Notification)
        .values(
            [
                {"user_id": u, "organisation_id": organisation_id, "kind": kind, "ref": ref}
                for u in sorted(members)
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "organisation_id", "kind", "ref"],
            index_where=Notification.read_at.is_(None),
        )
        .returning(Notification.user_id)
    ).all()
    changed(db, added, NEW)


def mark_read_about(
    db: Session, kind: str, ref: dict[str, Any], at: datetime | None = None
) -> None:
    """Mark every unread notification of `kind` whose reference contains `ref` read (as of
    `at`, else now), for every user: what it's about no longer needs them. In the caller's
    transaction."""
    read = db.scalars(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now() if at is None else at)
        .returning(Notification.user_id)
    ).all()
    changed(db, read, READ)


def mark_unread_about(db: Session, kind: str, ref: dict[str, Any], read_at: datetime) -> None:
    """Undo `mark_read_about` that marked them read as of `read_at`: the notifications it
    marked read (and only those) are unread again. In the caller's transaction."""
    unread = db.scalars(
        update(Notification)
        .where(
            Notification.kind == kind,
            Notification.ref.contains(ref),
            Notification.read_at == read_at,
        )
        .values(read_at=None)
        .returning(Notification.user_id)
    ).all()
    changed(db, unread, NEW)


def mark_read_in_chat(
    db: Session, organisation_id: int, user_id: int, chat_id: int, message_id: int
) -> None:
    """The person has read the organisation's chat up to `message_id`: their mention, reply and
    run failed notifications about its messages up to there are read. In the caller's
    transaction."""
    about = Notification.ref
    read = db.scalars(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.organisation_id == organisation_id,
            Notification.kind.in_(CHAT_KINDS),
            Notification.read_at.is_(None),
            cast(about["chat_id"].astext, BigInteger) == chat_id,
            cast(about["message_id"].astext, BigInteger) <= message_id,
        )
        .values(read_at=func.now())
        .returning(Notification.user_id)
    ).all()
    changed(db, read, READ)


def mark_read(db: Session, user_id: int, notification_ids: Iterable[uuid.UUID]) -> None:
    """Mark these of the person's notifications read; one already read keeps when it was first
    read. In the caller's transaction."""
    ids = list(notification_ids)
    if not ids:
        return
    read = db.scalars(
        update(Notification)
        .where(
            Notification.id.in_(ids),
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
        .returning(Notification.user_id)
    ).all()
    changed(db, read, READ)


def mark_all_read(db: Session, user_id: int) -> None:
    """Mark every one of the person's notifications read. In the caller's transaction."""
    read = db.scalars(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=func.now())
        .returning(Notification.user_id)
    ).all()
    changed(db, read, READ)


def forget_organisation(db: Session, organisation_id: int, user_id: int) -> None:
    """Delete the person's notifications from the organisation: they've left it. In the caller's
    transaction."""
    gone = db.scalars(
        delete(Notification)
        .where(Notification.user_id == user_id, Notification.organisation_id == organisation_id)
        .returning(Notification.user_id)
    ).all()
    changed(db, gone, READ)


def prune(now: datetime | None = None) -> int:
    """Delete notifications read more than `notification_retention_days` ago; how many went."""
    from aios.config import get_settings

    days = get_settings().notification_retention_days
    with SessionLocal() as db:
        cutoff = (now or db.scalar(select(func.now()))) - timedelta(days=days)
        pruned = db.execute(
            delete(Notification).where(
                Notification.read_at.is_not(None), Notification.read_at < cutoff
            )
        ).rowcount
        db.commit()
    return pruned


def notify_from_store(
    data_store_id: uuid.UUID, user_ids: Iterable[int], kind: str, ref: dict[str, Any]
) -> None:
    """`notify`, from the organisation whose store this is, in a platform transaction of its own
    (committed): for what a store-side job finds (`aios.verdicts`). Nothing if the store has no
    organisation any more."""
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        if organisation_id is None:
            return
        notify(db, user_ids, organisation_id, kind, ref)
        db.commit()


def admins(db: Session, organisation_id: int) -> list[int]:
    """The organisation's admins' user ids."""
    return list(
        db.scalars(
            select(OrganisationMember.user_id).where(
                OrganisationMember.organisation_id == organisation_id,
                OrganisationMember.role == Role.admin,
            )
        )
    )


def notify_admins_from_store(data_store_id: uuid.UUID, kind: str, ref: dict[str, Any]) -> None:
    """`notify_from_store`, for every admin of the organisation whose store this is."""
    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )
        if organisation_id is None:
            return
        notify(db, admins(db, organisation_id), organisation_id, kind, ref)
        db.commit()


# --- From the store side, once it commits --------------------------------------------------------


@dataclass(frozen=True)
class _Due:
    user_ids: tuple[int, ...]
    kind: str
    ref: dict[str, Any]
    # The chat whose stream, open in a visible tab, means a person needn't be told.
    unless_watching: int | None
    # The organisation's admins too.
    and_admins: bool = False


_DUE = "aios.notifications.due"


def after_store_commit(
    store: Session,
    user_ids: Iterable[int],
    kind: str,
    ref: dict[str, Any],
    *,
    unless_watching: int | None = None,
    and_admins: bool = False,
) -> None:
    """Notify the users of `kind` about `ref` once the store's transaction (`store`'s) commits,
    from its organisation; nothing if it rolls back. With `unless_watching` (a chat's id), not
    those watching that chat's stream in a visible tab then (`aios.streams.watching`). With
    `and_admins`, the organisation's admins too."""
    store.info.setdefault(_DUE, []).append(
        _Due(tuple(user_ids), kind, ref, unless_watching, and_admins)
    )


@sa_event.listens_for(Session, "after_commit")
def _deliver_after_store_commit(session: Session) -> None:
    due: list[_Due] | None = session.info.pop(_DUE, None)
    if not due:
        return
    from aios import streams
    from aios.datastores.connections import store_id_of

    try:
        store_id = store_id_of(session)
        with SessionLocal() as db:
            organisation_id = db.scalar(
                select(DataStore.owner_organisation_id).where(DataStore.id == store_id)
            )
            if organisation_id is None:
                return
            for d in due:
                users = [
                    u
                    for u in d.user_ids
                    if d.unless_watching is None
                    or not streams.watching(store_id, d.unless_watching, u)
                ]
                if d.and_admins:
                    users += admins(db, organisation_id)
                notify(db, users, organisation_id, d.kind, d.ref)
            db.commit()
    except Exception:
        log.exception("couldn't notify of what a store committed")


@sa_event.listens_for(Session, "after_rollback")
def _forget_after_store_rollback(session: Session) -> None:
    session.info.pop(_DUE, None)


# --- Telling people's streams --------------------------------------------------------------------

_CHANGED = "aios.notifications.changed"


def changed(db: Session, user_ids: Iterable[int], change: str) -> None:
    """Once `db` commits, tell each person's stream their notifications changed."""
    pending: dict[int, str] = db.info.setdefault(_CHANGED, {})
    for user_id in user_ids:
        # A new one among them is news; otherwise they were read.
        if pending.get(user_id) != NEW:
            pending[user_id] = change


@sa_event.listens_for(Session, "after_commit")
def _publish_changes(session: Session) -> None:
    pending: dict[int, str] | None = session.info.pop(_CHANGED, None)
    if not pending:
        return
    from aios import streams

    try:
        with SessionLocal() as db:
            counts = dict(
                db.execute(
                    select(Notification.user_id, func.count())
                    .where(Notification.user_id.in_(pending), Notification.read_at.is_(None))
                    .group_by(Notification.user_id)
                ).all()
            )
        streams.publish_many(
            (
                streams.person_channel(user_id),
                streams.encode(
                    "notification", {"change": change, "unread": counts.get(user_id, 0)}
                ),
            )
            for user_id, change in pending.items()
        )
    except Exception:
        log.exception("couldn't tell people's streams their notifications changed")


@sa_event.listens_for(Session, "after_rollback")
def _forget_changes(session: Session) -> None:
    session.info.pop(_CHANGED, None)


# --- What they say -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Description:
    # What it says, in a line.
    title: str
    # More, if there's more to say: an excerpt, what failed.
    detail: str | None = None


# Not found: what it was about no longer exists.
GONE = None

EXCERPT = 200


def excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1].rstrip() + "…"


def describe(
    db: Session, notifications: Sequence[Notification]
) -> dict[uuid.UUID, Description | None]:
    """What each notification says, by id, read from its organisation's store, a store at a
    time: `GONE` (None) for one whose reference no longer exists there, or whose organisation the
    person has left; left out for one that can't be read now (its store unreachable)."""
    from aios import notification_text

    return notification_text.describe(db, notifications)
