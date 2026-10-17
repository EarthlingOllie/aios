from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, object_session

from aios.db import SessionLocal
from aios.models import DataStore, User


def data_store_for(user: User) -> DataStore:
    """The user's data store, created if they don't have one yet.

    The only place a user becomes a store: teams will change how a store is picked here, and
    nothing below it. Blocking: call it from the threadpool in async code.

    If the session that loaded `user` is still open (a request's, a run's), the store is read on
    its connection, without touching that session: a caller holding one connection mustn't wait
    for another, or enough callers at once exhaust the pool waiting on each other. Creating a
    store commits, so that uses a session of its own and a store, once handed out, keeps its id;
    every user gets theirs on sign-in, so that's rare."""
    if (caller := object_session(user)) is not None:
        with Session(caller.connection(), expire_on_commit=False) as db:
            if (store := _find(db, user)) is not None:
                return store
    with SessionLocal() as db:
        if (store := _find(db, user)) is None:
            # Two first requests at once both get the one store.
            db.execute(
                insert(DataStore)
                .values(owner_user_id=user.id)
                .on_conflict_do_nothing(index_elements=[DataStore.owner_user_id])
            )
            db.commit()
            store = db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id)).one()
        return store


def _find(db: Session, user: User) -> DataStore | None:
    return db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
