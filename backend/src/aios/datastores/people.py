"""The people mirror: `aios.people` in each store, a copy of who is and was a member of its
organisation, which the platform keeps (`aios.datastores.models.Person`).

The platform is the source of truth for people and roles; the mirror is a derived copy, written
after the platform's own commit and idempotently: `sync` reads the organisation's members from
the platform and brings the mirror to match them, under a lock on the store's mirror so the
last to write read the latest. A person who is no longer a member keeps their row, with when
they left (`left_at`): what they made still names them, as a former member.

- **Who writes it.** Joining, leaving, renaming (and changing an email) and role changes mark the
  organisation's mirror as changed on the platform's session (`changed`); once that session
  commits, each is brought up to date (`refresh`). A store not ready yet is left to its
  provisioning, which fills it (`provision`), as the store migration command fills an existing
  store's once it has the table. A write that fails is logged, and left to the repair sweep.
- **The repair sweep** (`repair`, on the provisioning worker's beat) brings every ready store's
  mirror to match the memberships, and reports what it fixed: rows it added (a lost or deleted
  one), updated (a stale one) and marked left.
- **Reading it.** "Made by" and "changed by" resolve through it (`resolve`): a person's name, and
  whether they're a former member. Their email is on the deny list (`provision.DENIED_COLUMNS`),
  so the data tools never see it.
"""

import logging
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from aios.datastores import connections
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus, OrganisationMember, User

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Person:
    """A member as the platform has them: what their mirror row should say."""

    id: int
    first_name: str
    last_name: str
    email: str
    role: str
    joined_at: datetime


@dataclass
class Fixed:
    """What bringing a mirror up to date changed, by person id: rows added (missing), updated
    (stale) and marked left (no longer a member)."""

    added: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    left: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.left)


def members(db: Session, organisation_id: int) -> list[Person]:
    """The organisation's members, as the platform has them now."""
    rows = db.execute(
        select(User, OrganisationMember.role, OrganisationMember.joined_at)
        .join(OrganisationMember, OrganisationMember.user_id == User.id)
        .where(OrganisationMember.organisation_id == organisation_id)
        .order_by(User.id)
    ).all()
    return [
        Person(u.id, u.first_name, u.last_name, u.email, role.value, joined_at)
        for u, role, joined_at in rows
    ]


_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('aios.people', 0))"

_UPSERT = """
    INSERT INTO aios.people AS p (id, first_name, last_name, email, role, joined_at)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
        first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
        email = EXCLUDED.email, role = EXCLUDED.role, joined_at = EXCLUDED.joined_at,
        left_at = NULL, updated_at = now()
    WHERE (p.first_name, p.last_name, p.email, p.role, p.joined_at, p.left_at)
        IS DISTINCT FROM
        (EXCLUDED.first_name, EXCLUDED.last_name, EXCLUDED.email, EXCLUDED.role,
         EXCLUDED.joined_at, NULL::timestamptz)
    RETURNING id, xmax = 0 AS inserted
"""

_LEFT = """
    UPDATE aios.people SET left_at = now(), updated_at = now()
    WHERE left_at IS NULL AND NOT (id = ANY(%s))
    RETURNING id
"""


def sync(pg: psycopg.Connection, organisation_id: int) -> Fixed:
    """Bring the store's mirror (on `pg`, a connection to it allowed to write it) to match the
    organisation's members now, in one transaction; returns what it changed. Holds a lock on the
    mirror while it reads the platform and writes, so of two at once the later reads after the
    earlier wrote."""
    fixed = Fixed()
    with pg.transaction():
        pg.execute(_LOCK)
        with SessionLocal() as db:
            people = members(db, organisation_id)
        for p in people:
            row = pg.execute(
                _UPSERT, (p.id, p.first_name, p.last_name, p.email, p.role, p.joined_at)
            ).fetchone()
            if row is not None:
                (fixed.added if row[1] else fixed.updated).append(row[0])
        fixed.left = [r[0] for r in pg.execute(_LEFT, ([p.id for p in people],))]
    return fixed


def _ready_store(db: Session, organisation_id: int) -> uuid.UUID | None:
    return db.scalar(
        select(DataStore.id).where(
            DataStore.owner_organisation_id == organisation_id,
            DataStore.status == DataStoreStatus.ready,
        )
    )


def _sync_store(store_id: uuid.UUID, organisation_id: int) -> Fixed:
    with connections.store_session(store_id, "rw") as session:
        pg: psycopg.Connection = session.connection().connection.driver_connection
        # The session began a transaction already: `sync`'s is a savepoint in it.
        fixed = sync(pg, organisation_id)
        session.commit()
    return fixed


def refresh(organisation_id: int) -> Fixed | None:
    """Bring the organisation's store's mirror up to date, if its store is ready (one that isn't
    is filled when it's provisioned); returns what it changed, or None if there was no store to
    write."""
    with SessionLocal() as db:
        store_id = _ready_store(db, organisation_id)
    if store_id is None:
        return None
    return _sync_store(store_id, organisation_id)


# The organisations whose mirrors a session changed, to refresh once it commits.
_CHANGED = "aios.people_changed"


def changed(db: Session, organisation_ids: int | Iterable[int]) -> None:
    """Mark the organisations' mirrors as changed by `db`'s transaction (someone joined, left, was
    renamed or changed role): each is refreshed once it commits."""
    ids = [organisation_ids] if isinstance(organisation_ids, int) else organisation_ids
    db.info.setdefault(_CHANGED, set()).update(ids)


@event.listens_for(Session, "after_commit")
def _refresh_committed(session: Session) -> None:
    for organisation_id in sorted(session.info.pop(_CHANGED, ())):
        try:
            refresh(organisation_id)
        except Exception:
            # Not the caller's failure: the platform has committed. The repair sweep catches up.
            log.exception("Couldn't update organisation %s's people mirror", organisation_id)


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session: Session) -> None:
    session.info.pop(_CHANGED, None)


def repair(store_ids: Collection[uuid.UUID] | None = None) -> dict[uuid.UUID, Fixed]:
    """The repair sweep: bring every ready store's mirror (or only `store_ids`') to match its
    organisation's memberships, one store at a time; returns, and logs, what it fixed in each
    store it fixed something in. A store that fails is logged and left to the next sweep."""
    with SessionLocal() as db:
        query = select(DataStore.id, DataStore.owner_organisation_id).where(
            DataStore.status == DataStoreStatus.ready,
            DataStore.owner_organisation_id.is_not(None),
        )
        if store_ids is not None:
            query = query.where(DataStore.id.in_(store_ids))
        stores = db.execute(query.order_by(DataStore.created_at)).all()
    report: dict[uuid.UUID, Fixed] = {}
    for store_id, organisation_id in stores:
        try:
            fixed = _sync_store(store_id, organisation_id)
        except Exception:
            log.exception("Repairing data store %s's people mirror failed", store_id)
            continue
        if fixed:
            log.warning(
                "Repaired data store %s's people mirror: added %s, updated %s, marked left %s",
                store_id,
                fixed.added,
                fixed.updated,
                fixed.left,
            )
            report[store_id] = fixed
    return report


@dataclass(frozen=True)
class Named:
    """A person as the mirror names them."""

    id: int
    name: str  # "First Last"
    former: bool  # they've left the organisation


_RESOLVE = text(
    "SELECT id, first_name || ' ' || last_name AS name, left_at IS NOT NULL AS former"
    " FROM aios.people WHERE id = ANY(:ids)"
)


def resolve(db: Session, organisation_id: int, ids: Iterable[int | None]) -> dict[int, Named]:
    """The people with these ids (nulls skipped), by id, from the organisation's store's mirror;
    any it doesn't have (its store isn't ready, can't be reached, or hasn't caught up), from the
    platform: their name, and whether they've left (or were never a member)."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    found: dict[int, Named] = {}
    store_id = _ready_store(db, organisation_id)
    if store_id is not None:
        try:
            with connections.store_session(store_id, "ro") as store:
                for r in store.execute(_RESOLVE, {"ids": list(wanted)}):
                    found[r.id] = Named(r.id, r.name, r.former)
        except Exception:
            log.exception("Couldn't read data store %s's people mirror", store_id)
    missing = wanted - found.keys()
    if missing:
        current = set(
            db.scalars(
                select(OrganisationMember.user_id).where(
                    OrganisationMember.organisation_id == organisation_id,
                    OrganisationMember.user_id.in_(missing),
                )
            )
        )
        for u in db.scalars(select(User).where(User.id.in_(missing))):
            found[u.id] = Named(u.id, u.full_name, u.id not in current)
    return found
