"""Data stores: where users' data (their documents and chunks) lives.

Nothing reaches user data except through here. `data_store_for` turns a user into a store id,
and `store_session` opens a session on a store; everything in between takes a data store id.
For now every store is in the platform database.
"""

from aios.datastores.connections import DataStoreNotFound, Role, store_owner, store_session
from aios.datastores.resolve import data_store_for

__all__ = ["DataStoreNotFound", "Role", "data_store_for", "store_owner", "store_session"]
