import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from aios.db import SessionLocal, engine
from aios.models import DataStore

# The roles a store is opened as: `rw` writes (uploads, ingest), `ro` only reads (search).
# For now both are the platform's own connection; each store will get a login role of each kind.
StoreRole = Literal["rw", "ro"]


class DataStoreNotFound(LookupError):
    pass


class StoreSession(Session):
    """A session on one data store."""

    # The user who owns the store. While documents and chunks live in the platform database
    # they're keyed by owner, so this scopes them to the store; once each store has a database
    # of its own, that database is the scope.
    owner_user_id: int


_store_sessions = sessionmaker(engine, class_=StoreSession, expire_on_commit=False)


@contextmanager
def store_session(data_store_id: uuid.UUID, role: StoreRole = "rw") -> Iterator[StoreSession]:
    """A session on the data store's database, as `role`. The only way to reach a store's
    documents and chunks.

    For now every store is in the platform database, so `role` doesn't change anything yet."""
    with SessionLocal() as db:
        store = db.get(DataStore, data_store_id)
    if store is None:
        raise DataStoreNotFound(f"No data store {data_store_id}")
    with _store_sessions() as session:
        session.owner_user_id = store.owner_user_id
        yield session
