"""Data stores: where organisations' data lives, each a database of its own.

Nothing reaches their data except through here. `data_store_for` turns an organisation into a
store id (and creates the store, queuing its provisioning), and `store_session` opens a session
on a store's database; everything in between takes a data store id. The tables every store has
(documents and chunks) are in `aios.datastores.models`.
"""

from aios.datastores.connections import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    Role,
    ready_stores,
    store_conninfo,
    store_engine,
    store_id_of,
    store_session,
    store_status,
)
from aios.datastores.notices import store_busy, store_not_ready, store_upgrading
from aios.datastores.resolve import (
    can_approve,
    data_store_for,
    data_store_for_request,
    organisation_store,
)

__all__ = [
    "DataStoreBehind",
    "DataStoreBusy",
    "DataStoreNotFound",
    "DataStoreNotReady",
    "Role",
    "can_approve",
    "data_store_for",
    "data_store_for_request",
    "organisation_store",
    "ready_stores",
    "store_busy",
    "store_conninfo",
    "store_engine",
    "store_id_of",
    "store_not_ready",
    "store_upgrading",
    "store_session",
    "store_status",
]
