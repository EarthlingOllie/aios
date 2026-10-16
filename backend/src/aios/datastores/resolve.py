from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aios.db import SessionLocal
from aios.models import DataStore, User


def data_store_for(user: User) -> DataStore:
    """The user's data store, created if they don't have one yet.

    The only place a user becomes a store: teams will change how a store is picked here, and
    nothing below it. Uses its own session and commits, so a store, once handed out, keeps its id.
    Blocking: call it from the threadpool in async code."""
    with SessionLocal() as db:
        store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
        if store is None:
            # Two first requests at once both get the one store.
            db.execute(
                insert(DataStore)
                .values(owner_user_id=user.id)
                .on_conflict_do_nothing(index_elements=[DataStore.owner_user_id])
            )
            db.commit()
            store = db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id)).one()
        return store
