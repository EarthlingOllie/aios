"""Data stores: where users' data lives, and the only way code reaches it.

- `data_store_for(user)` is the only place a user becomes a store. Everything below it takes the
  store's id: the run context (`ChatDeps.data_store_id`), Celery tasks and the session opener.
- `store_session(data_store_id, role)` opens a session on a store, for its documents and chunks.
"""

from aios.datastores.connections import DataStoreNotFound, StoreRole, StoreSession, store_session
from aios.datastores.resolve import data_store_for

__all__ = ["DataStoreNotFound", "StoreRole", "StoreSession", "data_store_for", "store_session"]
