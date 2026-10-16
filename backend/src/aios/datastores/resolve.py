"""The one place a user becomes a data store."""

import logging
import uuid

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import DataStore, Document

log = logging.getLogger(__name__)

# Stores a session created, to provision once it commits (see `_provision_committed`).
_NEW_STORES = "aios.new_data_stores"


def names_for(store_id: uuid.UUID) -> tuple[str, str]:
    """A new store's database name and role prefix: from its id, never its owner's."""
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


def data_store_for(user_id: int, db: Session | None = None) -> uuid.UUID:
    """The id of the user's data store, creating it if they don't have one yet.

    A new store's row is `provisioning`, with its login roles' passwords (encrypted), and its
    provisioning is queued once the row is committed (`aios.datastores.provision`).

    Everything below this takes a data store id, not a user: when teams own stores, only how a
    caller picks its store changes.

    Given a session, it works in that session's transaction, and a row it creates is committed
    with the caller's work (so the caller must commit). Otherwise it uses and commits its own.
    """
    if db is None:
        with SessionLocal() as own:
            store_id = data_store_for(user_id, own)
            own.commit()
            return store_id
    store_id = db.scalar(select(DataStore.id).where(DataStore.owner_user_id == user_id))
    if store_id is None:
        new_id = uuid.uuid4()
        dbname, role_prefix = names_for(new_id)
        # Two first requests at once can both get here: the second's insert waits for the first
        # to commit and then does nothing, and its select finds the first's row.
        store_id = db.scalar(
            insert(DataStore)
            .values(
                id=new_id,
                owner_user_id=user_id,
                dbname=dbname,
                role_prefix=role_prefix,
                rw_password=credentials.encrypt(credentials.new_password()),
                ro_password=credentials.encrypt(credentials.new_password()),
            )
            .on_conflict_do_nothing(index_elements=[DataStore.owner_user_id])
            .returning(DataStore.id)
        )
        if store_id is None:
            store_id = db.scalar(select(DataStore.id).where(DataStore.owner_user_id == user_id))
        else:
            db.info.setdefault(_NEW_STORES, []).append(store_id)
    return store_id


def data_store_of_document(document_id: int) -> uuid.UUID | None:
    """The store a document is in; None if it's gone.

    Only for ingest messages queued before data stores (`aios.worker`), which carry just the
    document's id; remove it once a release has drained them.
    """
    with SessionLocal() as db:
        owner_id = db.scalar(select(Document.owner_id).where(Document.id == document_id))
        if owner_id is None:
            return None
        store_id = data_store_for(owner_id, db)
        db.commit()
        return store_id
