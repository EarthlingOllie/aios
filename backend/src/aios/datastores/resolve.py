"""The one place an organisation becomes a data store, and who may decide what it holds."""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.datastores import credentials
from aios.datastores.connections import Role, store_session
from aios.db import SessionLocal
from aios.models import DataStore, OrganisationMember
from aios.models import Role as MemberRole

log = logging.getLogger(__name__)

# Stores a session created, to provision once it commits (see `_provision_committed`).
_NEW_STORES = "aios.new_data_stores"


def names_for(store_id: uuid.UUID) -> tuple[str, str]:
    """A new store's database name and role prefix: from its id, never its organisation's."""
    name = f"ds_{store_id.hex}"
    return name, name


def enqueue_provisioning(store_id: uuid.UUID) -> None:
    """Queue the store's provisioning on the provisioning worker."""
    from aios.worker import provision_data_store_task

    provision_data_store_task.delay(str(store_id))


@event.listens_for(Session, "after_commit")
def _provision_committed(session: Session) -> None:
    for store_id in session.info.pop(_NEW_STORES, ()):
        try:
            enqueue_provisioning(store_id)
        except Exception:
            # Not the caller's failure: the store stays `provisioning`, which
            # `provision.retry_unfinished` finishes (or the deploy's `migrate_all`).
            log.exception("Couldn't queue provisioning data store %s", store_id)


@event.listens_for(Session, "after_rollback")
def _forget_rolled_back(session: Session) -> None:
    session.info.pop(_NEW_STORES, None)


def can_approve(db: Session, user_id: int, store: DataStore) -> bool:
    """Whether the user may decide the store's schema change requests (and restore what drops
    took): a permission, not who the store is. Those in `approvers`."""
    return user_id in approvers(db, store)


def approvers(db: Session, store: DataStore, least: MemberRole | None = None) -> list[int]:
    """The users who see the store's schema change requests (`can_approve`): every member of
    its organisation, in the order they joined. With `least`, only those whose role is it or
    above (`aios.approval_rules`): whom a request needing it notifies."""
    query = select(OrganisationMember.user_id).where(
        OrganisationMember.organisation_id == store.owner_organisation_id
    )
    if least is not None:
        from aios.approval_rules import RANK

        query = query.where(
            OrganisationMember.role.in_([r for r in RANK if RANK[r] >= RANK[least]])
        )
    return list(
        db.scalars(query.order_by(OrganisationMember.joined_at, OrganisationMember.user_id))
    )


def data_store_for(organisation_id: int, db: Session | None = None) -> uuid.UUID:
    """The id of the organisation's data store, creating it if it doesn't have one yet.

    A new store's row is `provisioning`, with its login roles' passwords (encrypted), and its
    provisioning is queued once the row is committed (`aios.datastores.provision`). Signing up
    makes the new organisation's (`aios.auth`); anything else that needs it makes it otherwise.

    Everything below this takes a data store id, not an organisation: every member's requests,
    and runs, reach the one store.

    Given a session, it works in that session's transaction, and a row it creates is committed
    with the caller's work (so the caller must commit). Otherwise it uses and commits its own.
    """
    if db is None:
        with SessionLocal() as own:
            store_id = data_store_for(organisation_id, own)
            own.commit()
            return store_id
    owned = DataStore.owner_organisation_id == organisation_id
    store_id = db.scalar(select(DataStore.id).where(owned))
    if store_id is None:
        new_id = uuid.uuid4()
        dbname, role_prefix = names_for(new_id)
        # Two first requests at once can both get here: the second's insert waits for the first
        # to commit and then does nothing, and its select finds the first's row.
        store_id = db.scalar(
            insert(DataStore)
            .values(
                id=new_id,
                owner_organisation_id=organisation_id,
                dbname=dbname,
                role_prefix=role_prefix,
                rw_password=credentials.encrypt(credentials.new_password()),
                ro_password=credentials.encrypt(credentials.new_password()),
            )
            .on_conflict_do_nothing(index_elements=[DataStore.owner_organisation_id])
            .returning(DataStore.id)
        )
        if store_id is None:
            store_id = db.scalar(select(DataStore.id).where(owned))
        else:
            db.info.setdefault(_NEW_STORES, []).append(store_id)
    return store_id


@contextmanager
def organisation_store(organisation_id: int, role: Role = "rw") -> Iterator[Session]:
    """A session on the organisation's store (`data_store_for`, then `store_session`), as `role`,
    closed on exit: where its agents, skills and workflows are. Raises as `store_session` does
    (a new store isn't ready)."""
    with store_session(data_store_for(organisation_id), role) as store:
        yield store


def data_store_for_request(organisation_id: int, db: Session) -> uuid.UUID:
    """The signed-in user's organisation's data store, for an endpoint that reaches data only
    through it.

    Resolved on `db`, the request's session (which loaded the organisation), which is then
    committed (queuing a new store's provisioning) and closed, returning its connection to the
    pool before the store is opened. Held while a store session waits for a connection, enough
    requests at once would exhaust the pool waiting on each other. Blocking: call it from the
    threadpool in async code."""
    store_id = data_store_for(organisation_id, db)
    db.commit()
    db.close()
    return store_id
