"""Opening sessions on a data store.

For now every store is in the platform database, so a store's session is a platform session,
and a store's rows are told apart there by the user who owns them (`store_owner`). Once stores
have databases of their own, this is the only module that changes: it connects to the store's
database, as the role asked for, and `store_owner` goes.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from aios.db import SessionLocal
from aios.models import DataStore

# Read-write, or read-only (search). Both are the platform database's role for now.
Role = Literal["rw", "ro"]

_OWNER = "data_store_owner_id"


class DataStoreNotFound(LookupError):
    """No data store has this id."""


@contextmanager
def store_session(data_store_id: uuid.UUID, role: Role = "rw") -> Iterator[Session]:
    """A session on the data store, closed on exit. Raises `DataStoreNotFound` if there's no
    such store."""
    if role not in get_args(Role):
        raise ValueError(f"Unknown data store role: {role!r}")
    with SessionLocal() as db:
        owner_id = db.scalar(select(DataStore.owner_user_id).where(DataStore.id == data_store_id))
        if owner_id is None:
            raise DataStoreNotFound(data_store_id)
        # End the lookup's transaction, so the caller starts from a clean session.
        db.commit()
        db.info[_OWNER] = owner_id
        yield db


def store_owner(db: Session) -> int:
    """The user whose rows in the platform database are the store's (see the module docstring).

    Only for sessions from `store_session`."""
    return db.info[_OWNER]
