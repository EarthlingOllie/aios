"""Data stores: where users' data lives, each a database of its own.

Nothing reaches user data except through here. `data_store_for` turns a user into a store id
(and creates the store, queuing its provisioning), and `store_session` opens a session on a
store's database; everything in between takes a data store id. Documents and chunks are still
in the platform database for now (`documents_session`).
"""

from aios.datastores.connections import (
    DataStoreNotFound,
    DataStoreNotReady,
    Role,
    documents_session,
    store_owner,
    store_session,
    store_status,
)
from aios.datastores.notices import store_not_ready
from aios.datastores.resolve import data_store_for

__all__ = [
    "DataStoreNotFound",
    "DataStoreNotReady",
    "Role",
    "data_store_for",
    "documents_session",
    "store_not_ready",
    "store_owner",
    "store_session",
    "store_status",
]
