"""The one place a user becomes a data store."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aios.db import SessionLocal
from aios.models import DataStore


def data_store_for(user_id: int) -> uuid.UUID:
    """The id of the user's data store, creating its row if they don't have one yet.

    Everything below this takes a data store id, not a user: when teams own stores, only how a
    caller picks its store changes.
    """
    with SessionLocal() as db:
        store_id = db.scalar(select(DataStore.id).where(DataStore.owner_user_id == user_id))
        if store_id is None:
            # Two first requests at once can both get here; the second's insert does nothing.
            db.execute(
                insert(DataStore)
                .values(owner_user_id=user_id)
                .on_conflict_do_nothing(index_elements=[DataStore.owner_user_id])
            )
            db.commit()
            store_id = db.scalar(select(DataStore.id).where(DataStore.owner_user_id == user_id))
        return store_id
