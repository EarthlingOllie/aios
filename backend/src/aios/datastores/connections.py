"""Opening sessions on a data store.

`store_session(data_store_id, role)` opens a session on the store's own database, as its
read-write (`rw`) or read-only (`ro`) role, once the store is ready. Engines are cached per
(store, role), each with a small pool, and the least recently used are disposed of beyond
`MAX_ENGINES`.

Until documents and chunks move into the stores' databases, they're still in the platform
database, told apart there by the user who owns them: `documents_session` opens a platform
session for them (also only once the store is ready, as they'll need it), and `store_owner`
says whose rows are the store's. Both go once documents move.
"""

import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, get_args

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus

# Read-write, or read-only (search, and queries agents write).
Role = Literal["rw", "ro"]

# Engines kept open at once (each up to 3 connections); past it the least recently used goes.
MAX_ENGINES = 64

_OWNER = "data_store_owner_id"


class DataStoreNotFound(LookupError):
    """No data store has this id."""


class DataStoreNotReady(RuntimeError):
    """The data store is still being set up (or setting it up failed)."""

    def __init__(self, data_store_id: uuid.UUID, status: DataStoreStatus):
        super().__init__(f"Data store {data_store_id} is {status.value}")
        self.data_store_id = data_store_id
        self.status = status


def _check_role(role: str) -> None:
    if role not in get_args(Role):
        raise ValueError(f"Unknown data store role: {role!r}")


def _ready_store(db: Session, data_store_id: uuid.UUID) -> DataStore:
    store = db.get(DataStore, data_store_id)
    if store is None:
        raise DataStoreNotFound(data_store_id)
    if store.status != DataStoreStatus.ready:
        raise DataStoreNotReady(data_store_id, store.status)
    return store


_engines: OrderedDict[tuple[uuid.UUID, str], Engine] = OrderedDict()
_engines_lock = threading.Lock()


def _engine(store: DataStore, role: Role) -> Engine:
    key = (store.id, role)
    with _engines_lock:
        engine = _engines.get(key)
        if engine is not None:
            _engines.move_to_end(key)
            return engine
        password = store.rw_password if role == "rw" else store.ro_password
        url = URL.create(
            "postgresql+psycopg",
            username=f"{store.role_prefix}_{role}",
            password=credentials.decrypt(password),
            host=store.host,
            port=store.port,
            database=store.dbname,
        )
        engine = create_engine(url, pool_size=1, max_overflow=2, pool_pre_ping=True)
        _engines[key] = engine
        while len(_engines) > MAX_ENGINES:
            _, evicted = _engines.popitem(last=False)
            evicted.dispose()
        return engine


def dispose_engines() -> None:
    """Close every cached store engine (e.g. in tests, before dropping a store)."""
    with _engines_lock:
        while _engines:
            _, engine = _engines.popitem()
            engine.dispose()


@contextmanager
def store_session(data_store_id: uuid.UUID, role: Role = "rw") -> Iterator[Session]:
    """A session on the data store's database, as its `role`, closed on exit.

    Raises `DataStoreNotFound` if there's no such store, and `DataStoreNotReady` until it's
    ready."""
    _check_role(role)
    with SessionLocal() as db:
        store = _ready_store(db, data_store_id)
    with Session(_engine(store, role)) as session:
        yield session


@contextmanager
def documents_session(data_store_id: uuid.UUID, role: Role = "rw") -> Iterator[Session]:
    """A platform session for the store's documents and chunks, which are still in the
    platform database (see the module docstring), closed on exit.

    Raises as `store_session` does."""
    _check_role(role)
    with SessionLocal() as db:
        owner_id = _ready_store(db, data_store_id).owner_user_id
        # End the lookup's transaction, so the caller starts from a clean session.
        db.commit()
        db.info[_OWNER] = owner_id
        yield db


def store_owner(db: Session) -> int:
    """The user whose documents in the platform database are the store's (see the module
    docstring).

    Only for sessions from `documents_session`."""
    return db.info[_OWNER]


def store_status(data_store_id: uuid.UUID) -> DataStoreStatus:
    """The store's status. Raises `DataStoreNotFound` if there's no such store."""
    with SessionLocal() as db:
        status = db.scalar(select(DataStore.status).where(DataStore.id == data_store_id))
    if status is None:
        raise DataStoreNotFound(data_store_id)
    return status
