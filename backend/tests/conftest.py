import os
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from aios.auth import current_user
from aios.datastores import data_store_for, provision, resolve
from aios.datastores.connections import dispose_engines
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import DataStore, DataStoreStatus, User
from aios.sandbox import runner


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db():
    """A session against a migrated database (`alembic upgrade head`); skips if unavailable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM chunks LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        # The server is up but won't let DATABASE_URL's role in: the roles haven't been created
        # (or the password is wrong), which skipping would hide.
        message = str(exc)
        if isinstance(exc, OperationalError) and (
            "password authentication failed" in message
            or ('role "' in message and "does not exist" in message)
        ):
            pytest.fail(
                f"DATABASE_URL's role can't log in ({exc.orig}). Create the roles with "
                "`python -m aios.db_roles` (see the README's Database roles)."
            )
        if isinstance(exc, OperationalError) or "chunks" in str(exc):
            pytest.skip(f"database not available: {exc.__class__.__name__}")
        raise
    with SessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def provisioning(monkeypatch) -> list[uuid.UUID]:
    """Data stores whose provisioning was queued, instead of sending it to the broker."""
    queued: list[uuid.UUID] = []
    monkeypatch.setattr(resolve, "enqueue_provisioning", queued.append)
    return queued


def delete_user(db, user: User) -> None:
    """Delete the user, and drop their store's database if it was provisioned."""
    stores = [
        provision.StoreNames.of(s)
        for s in db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id))
    ]
    db.delete(user)
    db.commit()
    if stores:
        dispose_engines()
        for names in stores:
            provision.drop(names)


def mark_ready(db, user: User) -> uuid.UUID:
    """Give the user a store that's ready, without provisioning it: enough for documents and
    search, which are still in the platform database."""
    store_id = data_store_for(user.id)
    db.execute(
        update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.ready)
    )
    db.commit()
    return store_id


@pytest.fixture
def user(db):
    """A user whose store is ready (not provisioned: see `mark_ready`)."""
    u = User(github_id=-1, login="test-user")
    db.add(u)
    db.commit()
    mark_ready(db, u)
    yield u
    delete_user(db, u)


@pytest.fixture
def new_user(db) -> Iterator[Callable[[], User]]:
    """Makes users with no store yet; deletes them (and drops their stores) afterwards."""
    made: list[User] = []

    def make() -> User:
        u = User(github_id=-(1000 + len(made)), login=f"new-user-{len(made)}")
        db.add(u)
        db.commit()
        made.append(u)
        return u

    yield make
    for u in made:
        delete_user(db, u)


@pytest.fixture
def api(client, user):
    """A client signed in as `user`."""
    app.dependency_overrides[current_user] = lambda: user
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def workdir(monkeypatch):
    # Not under pytest's tmp_path, whose parents only its owner can enter: run as root, the
    # runner runs the code as another user, which needs to reach the working directory.
    with tempfile.TemporaryDirectory() as parent:
        os.chmod(parent, 0o755)
        work = Path(parent) / "work"
        monkeypatch.setattr(runner, "WORKDIR", str(work))
        runner.prepare_workdir()
        yield work
