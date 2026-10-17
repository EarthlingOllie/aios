import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from aios.auth import current_user
from aios.datastores import data_store_for, provision, resolve
from aios.datastores.connections import dispose_engines
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import DataStore, User
from aios.sandbox import runner


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db():
    """A session against a migrated database (`alembic upgrade head`); skips if unavailable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM data_stores LIMIT 1"))
    except OperationalError as exc:
        # libpq prefixes an error the server sent with its severity ("FATAL:  ..."), so one
        # without it never reached a server (refused, unresolvable host, timeout): skip. One with
        # it is a server that's up but turns DATABASE_URL away (the role or database is missing,
        # a wrong password, no CONNECT privilege, a pg_hba.conf rule), which skipping would hide.
        if "FATAL:" in str(exc.orig):
            pytest.fail(
                f"the database server turned DATABASE_URL away ({exc.orig}). If its role is "
                "missing, create the roles with `python -m aios.db_roles` (see the README's "
                "Database roles)."
            )
        pytest.skip(f"database not available: {exc.__class__.__name__}")
    except ProgrammingError as exc:
        if isinstance(exc.orig, errors.UndefinedTable):
            pytest.skip("database not migrated (`alembic upgrade head`)")
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


def provisioned_user(db, github_id: int, login: str) -> User:
    """A new user, with their store provisioned: a database of its own, ready. Delete them with
    `delete_user`."""
    u = User(github_id=github_id, login=login)
    db.add(u)
    db.commit()
    provision.provision(data_store_for(u.id))
    return u


@pytest.fixture
def user(db):
    """A user whose store is provisioned and ready."""
    u = provisioned_user(db, -1, "test-user")
    yield u
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
