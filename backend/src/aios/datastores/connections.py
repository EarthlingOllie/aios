"""Opening sessions on a data store.

`store_session(data_store_id, role)` opens a session on the store's own database, as its
read-write (`rw`) or read-only (`ro`) role, once the store is ready. Engines are cached per
(store, role), each with a small pool, and the least recently used are disposed of beyond
`MAX_ENGINES`, so hold an engine (or session) no longer than the work on it: one disposed of
while held starts a new pool nothing here tracks. A caller that wants to know the store is
ready before something costly (search, before it pays to embed the query) checks
`store_status` first, then opens its session.

Store sessions go through the connection pooler when one is configured
(`data_store_pooler_address`): PgBouncer in transaction mode, which may hand each transaction a
different server connection. So nothing here leaves session state behind: no prepared
statements (psycopg's are off), no settings at connection startup (the roles' own defaults
carry them), and no session advisory locks.

Each login role may hold only so many connections (`data_store_connection_limit`, a backstop
behind the pooler's own pools). Past it, or when this process's pool on the store stays full for
`POOL_TIMEOUT` seconds, opening a connection raises `DataStoreBusy`, which callers can retry.
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
from aios.datastores import credentials, migrations
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus

# Read-write, or read-only (search, and queries agents write).
Role = Literal["rw", "ro"]

# Engines kept open at once (each up to 5 connections); past it the least recently used goes.
MAX_ENGINES = 64
# The connections each store engine pools, at most: one kept, four more while busy.
POOL_SIZE, MAX_OVERFLOW = 1, 4
MAX_CONNECTIONS = POOL_SIZE + MAX_OVERFLOW

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


class DataStoreBehind(DataStoreNotReady):
    """The store is ready, but at a revision of the store tree before its head, which this
    release's code needs (the release before made it during a deploy, or migrating it failed):
    worth trying again once the schedule brings it there (`provision.retry_unfinished`)."""

    def __init__(self, data_store_id: uuid.UUID, revision: str):
        super().__init__(data_store_id, DataStoreStatus.ready)
        self.args = (f"Data store {data_store_id} is at {revision}, before the tree's head",)
        self.revision = revision


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
    # Not a revision this release doesn't know: the next release has already migrated it.
    if store.schema_revision in migrations.revisions_before_head():
        raise DataStoreBehind(data_store_id, store.schema_revision)
    return store


def store_url(store: DataStore, role: Role) -> URL:
    """Where the app reaches the store's database, as its `role`: at the pooler, if one is
    configured (`data_store_pooler_address`), which passes the role and database on.

    Otherwise, a store with no host of its own is on the platform database's instance, so it's
    reached at DATABASE_URL's address, with its options (e.g. `sslmode`): where the provisioner
    reached it from may not resolve here (Compose's `db`, with the API on the host)."""
    settings = get_settings()
    url = make_url(settings.database_url)
    if settings.data_store_pooler_address:
        host, _, port = settings.data_store_pooler_address.rpartition(":")
        url = url.set(host=host, port=int(port))
    elif store.host is not None:
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
        # Up to `MAX_CONNECTIONS` per (store, role) in each process. psycopg mustn't prepare the
        # statements it runs often: through the pooler, the next transaction may be on a server
        # connection that never saw them (and `query_data` resets read-only connections with
        # `DISCARD ALL`, which drops them).
        connect_args = {"prepare_threshold": None}
        engine = create_engine(
            store_url(store, role),
            poolclass=_pool_class(store.id),
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_timeout=POOL_TIMEOUT,
            pool_pre_ping=True,
            connect_args=connect_args,
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


def dispose_engine(data_store_id: uuid.UUID, role: Role) -> None:
    """Close the store's cached engine as `role`, if there is one: for a process that visits
    every store in turn (measuring them), and would otherwise hold a connection to each."""
    with _engines_lock:
        engine = _engines.pop((data_store_id, role), None)
    if engine is not None:
        engine.dispose()


def store_engine(data_store_id: uuid.UUID, role: Role = "rw") -> Engine:
    """The engine on the data store's database, as its `role`.

    Raises `DataStoreNotFound` if there's no such store, and `DataStoreNotReady` until it's
    ready (`DataStoreBehind` while it's behind the store tree's head)."""
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
        session.info[_STORE_ID] = data_store_id
        yield session


_STORE_ID = "aios.data_store_id"


def store_id_of(session: Session) -> uuid.UUID:
    """The id of the data store a `store_session` is a session on: e.g. to name a run of it
    outside the store, where ids repeat across stores."""
    return session.info[_STORE_ID]


def store_conninfo(data_store_id: uuid.UUID, role: Role = "rw") -> str:
    """Where to connect to the data store's database as its `role`, as libpq connection info,
    for what connects with psycopg rather than through an engine (the checkpointer,
    `aios.checkpoints`): through the pooler when one is configured, as `store_url` says. Its
    caller mustn't leave session state behind (see the module docstring).

    Raises as `store_engine` does."""
    _check_role(role)
    with SessionLocal() as db:
        store = _ready_store(db, data_store_id)
    url = store_url(store, role).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def store_status(data_store_id: uuid.UUID) -> DataStoreStatus:
    """The store's status. Raises `DataStoreNotFound` if there's no such store."""
    with SessionLocal() as db:
        status = db.scalar(select(DataStore.status).where(DataStore.id == data_store_id))
    if status is None:
        raise DataStoreNotFound(data_store_id)
    return status
