"""Opening sessions on a data store.

`store_session(data_store_id, role)` opens a session on the store's own database, as its
read-write (`rw`) or read-only (`ro`) role, once the store is ready. Engines are cached per
(store, role), each with a small pool, and the least recently used are disposed of beyond
`MAX_ENGINES`. `store_engine` is the lookup on its own, for a caller that wants to know the
store is ready before doing something costly (search, before it pays to embed the query).

Each login role may hold only so many connections (`data_store_connection_limit`). Past it, or
when this process's pool on the store stays full for `POOL_TIMEOUT` seconds, opening a
connection raises `DataStoreBusy`, which callers can retry.
"""

import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, get_args

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.engine import URL, ExceptionContext, make_url
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session
from sqlalchemy.pool import PoolProxiedConnection, QueuePool

from aios.config import get_settings
from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus

# Read-write, or read-only (search, and queries agents write).
Role = Literal["rw", "ro"]

# Engines kept open at once (each up to 3 connections); past it the least recently used goes.
MAX_ENGINES = 64

# Seconds to wait for one of those connections when all are in use.
POOL_TIMEOUT = 30


class DataStoreNotFound(LookupError):
    """No data store has this id."""


class DataStoreNotReady(RuntimeError):
    """The data store is still being set up (or setting it up failed)."""

    def __init__(self, data_store_id: uuid.UUID, status: DataStoreStatus):
        super().__init__(f"Data store {data_store_id} is {status.value}")
        self.data_store_id = data_store_id
        self.status = status


class DataStoreBusy(RuntimeError):
    """The store's role already holds all the connections it may (its `CONNECTION LIMIT`), or
    its instance does, or this process's pool on it does: worth trying again shortly."""

    def __init__(self, data_store_id: uuid.UUID):
        super().__init__(f"Data store {data_store_id} has no connections free")
        self.data_store_id = data_store_id


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


def store_url(store: DataStore, role: Role) -> URL:
    """Where the app reaches the store's database, as its `role`.

    A store with no host of its own is on the platform database's instance, so it's reached at
    DATABASE_URL's address, with its options (e.g. `sslmode`): where the provisioner reached it
    from may not resolve here (Compose's `db`, with the API on the host)."""
    url = make_url(get_settings().database_url)
    if store.host is not None:
        url = url.set(host=store.host, port=store.port)
    password = store.rw_password if role == "rw" else store.ro_password
    return url.set(
        username=f"{store.role_prefix}_{role}",
        password=credentials.decrypt(password),
        database=store.dbname,
    )


_engines: OrderedDict[tuple[uuid.UUID, str], Engine] = OrderedDict()
_engines_lock = threading.Lock()


# How Postgres refuses a connection past a limit: the role's, or the whole instance's
# (`max_connections`, less the slots it keeps for superusers), which frees up just the same.
_REFUSALS = (
    "too many connections for role",
    "sorry, too many clients already",
    "remaining connection slots are reserved",
)


def _busy(data_store_id: uuid.UUID):
    def handle_error(context: ExceptionContext) -> BaseException | None:
        # By the text: psycopg gives a FATAL at connect time no SQLSTATE to check (all three are
        # 53300), and it's in English unless the server's `lc_messages` says otherwise.
        message = str(context.original_exception)
        if any(refusal in message for refusal in _REFUSALS):
            return DataStoreBusy(data_store_id)
        return None

    return handle_error


def _pool_class(data_store_id: uuid.UUID) -> type[QueuePool]:
    # A pool that stays full raises `PoolTimeout`, which isn't a database error, so `_busy`
    # never sees it.
    class StorePool(QueuePool):
        def connect(self) -> PoolProxiedConnection:
            try:
                return super().connect()
            except PoolTimeout as exc:
                raise DataStoreBusy(data_store_id) from exc

    return StorePool


def _engine(store: DataStore, role: Role) -> Engine:
    key = (store.id, role)
    with _engines_lock:
        engine = _engines.get(key)
        if engine is not None:
            _engines.move_to_end(key)
            return engine
        # Up to 3 connections per (store, role) in each process: the roles' connection limit is
        # budgeted from it (`data_store_connection_limit`).
        engine = create_engine(
            store_url(store, role),
            poolclass=_pool_class(store.id),
            pool_size=1,
            max_overflow=2,
            pool_timeout=POOL_TIMEOUT,
            pool_pre_ping=True,
        )
        event.listen(engine, "handle_error", _busy(store.id))
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


def store_engine(data_store_id: uuid.UUID, role: Role = "rw") -> Engine:
    """The engine on the data store's database, as its `role`.

    Raises `DataStoreNotFound` if there's no such store, and `DataStoreNotReady` until it's
    ready."""
    _check_role(role)
    with SessionLocal() as db:
        store = _ready_store(db, data_store_id)
    return _engine(store, role)


@contextmanager
def store_session(data_store_id: uuid.UUID, role: Role = "rw") -> Iterator[Session]:
    """A session on the data store's database, as its `role`, closed on exit.

    Raises as `store_engine` does, and `DataStoreBusy` if the role has no connections free."""
    # Like the platform's sessions (`aios.db`), rows stay readable after a commit.
    with Session(store_engine(data_store_id, role), expire_on_commit=False) as session:
        yield session


def store_status(data_store_id: uuid.UUID) -> DataStoreStatus:
    """The store's status. Raises `DataStoreNotFound` if there's no such store."""
    with SessionLocal() as db:
        status = db.scalar(select(DataStore.status).where(DataStore.id == data_store_id))
    if status is None:
        raise DataStoreNotFound(data_store_id)
    return status
