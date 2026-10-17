"""Data stores: where users' data lives, each a database of its own.

Nothing reaches user data except through here. `data_store_for` turns a user into a store id
(and creates the store, queuing its provisioning), and `store_session` opens a session on a
store's database; everything in between takes a data store id. The tables every store has
(documents and chunks) are in `aios.datastores.models`.
"""

from aios.datastores.connections import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    Role,
    store_engine,
    store_session,
    store_status,
)
from aios.datastores.notices import store_not_ready
from aios.datastores.resolve import data_store_for

__all__ = [
    "DataStoreBehind",
    "DataStoreBusy",
    "DataStoreNotFound",
    "DataStoreNotReady",
    "Role",
    "data_store_for",
    "store_engine",
    "store_not_ready",
    "store_session",
    "store_status",
]
