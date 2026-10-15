"""The one place a user becomes a data store."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios.db import SessionLocal
from aios.models import DataStore


def data_store_for(user_id: int, db: Session | None = None) -> uuid.UUID:
    """The id of the user's data store, creating its row if they don't have one yet.

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
        # Two first requests at once can both get here: the second's insert waits for the first
        # to commit and then does nothing, and its select finds the first's row.
        store_id = db.scalar(
            insert(DataStore)
            .values(owner_user_id=user_id)
            .on_conflict_do_nothing(index_elements=[DataStore.owner_user_id])
            .returning(DataStore.id)
        ) or db.scalar(select(DataStore.id).where(DataStore.owner_user_id == user_id))
    return store_id
