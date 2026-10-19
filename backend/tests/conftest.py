import asyncio
import io
import os
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import fake_model
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from aios import checkpoints, organisations
from aios.auth import current_user
from aios.config import get_settings
from aios.datastores import data_store_for, files, history, provision, resolve, store_session
from aios.datastores.connections import dispose_engines
from aios.datastores.models import Document
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import DataStore, Organisation, OrganisationMember, User
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
    # Before any test holds a transaction open: on a fresh database the checkpointer's setup
    # creates its indexes CONCURRENTLY, which waits for every older transaction in the
    # database, so the first turn would otherwise wait for ever on a test's own open session.
    asyncio.run(checkpoints.ensure_ready())
    with SessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def provisioning(monkeypatch) -> list[uuid.UUID]:
    """Data stores whose provisioning was queued, instead of sending it to the broker."""
    queued: list[uuid.UUID] = []
    monkeypatch.setattr(resolve, "enqueue_provisioning", queued.append)
    return queued


@pytest.fixture(autouse=True)
def handoff_model() -> Iterator[None]:
    """The handoff step's model (`aios.handoff`), scripted rather than the provider's: it
    acknowledges with "On it." and briefs nothing (`fake_model.use_handoff` scripts another).
    Not with `monkeypatch`, which a test may undo."""
    with fake_model.use_handoff(fake_model.briefs()):
        yield


@pytest.fixture(autouse=True)
def history_tasks(monkeypatch) -> dict[str, list[uuid.UUID]]:
    """The run history export's tasks the code queued (by store), instead of sending them to
    the broker: "export" drains a store's outbox, "set_up" gives it the `history` tables."""
    queued: dict[str, list[uuid.UUID]] = {"export": [], "set_up": []}
    monkeypatch.setattr(history, "enqueue_export", queued["export"].append)
    monkeypatch.setattr(history, "enqueue_set_up", queued["set_up"].append)
    return queued


def delete_user(db, user: User) -> None:
    """Delete the user's organisation (cascading to what it owns), the user, and their store's
    database if it was provisioned."""
    stores = [
        provision.StoreNames.of(s)
        for s in db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id))
    ]
    for org in db.scalars(
        select(Organisation).join(OrganisationMember).where(OrganisationMember.user_id == user.id)
    ).all():
        db.delete(org)
    db.delete(user)
    db.commit()
    if stores:
        dispose_engines()
        for names in stores:
            provision.drop(names)


def sign_up(db, github_id: int, login: str, **fields) -> User:
    """A new user in their personal organisation, as signing in leaves them, with no store yet.
    Delete them with `delete_user`."""
    u = User(github_id=github_id, login=login, **fields)
    db.add(u)
    db.flush()
    organisations.create_personal(db, u)
    db.commit()
    return u


def provisioned_user(db, github_id: int, login: str) -> User:
    """A new user, with their store provisioned: a database of its own, ready. Delete them with
    `delete_user`."""
    u = sign_up(db, github_id, login)
    provision.provision(data_store_for(u.id))
    return u


@pytest.fixture
def user(db):
    """A user whose store is provisioned and ready."""
    u = provisioned_user(db, -1, "test-user")
    yield u
    delete_user(db, u)


@pytest.fixture(scope="session")
def files_bucket() -> str:
    """The bucket uploads go to (on RustFS, locally and in CI), made if it's missing."""
    try:
        files.create_bucket()
    except (BotoCoreError, ClientError) as exc:
        pytest.fail(
            f"Can't reach the files bucket ({exc}). Start RustFS and point "
            "DATA_STORE_FILES_ENDPOINT and DATA_STORE_FILES_ACCESS_KEY/_SECRET_KEY at it "
            "(see the README's Checks)."
        )
    return get_settings().data_store_files_bucket


def add_document(store_id: uuid.UUID, data: bytes = b"# Hi", **values) -> int:
    """A document in the store, with its file in S3, as an upload leaves it; returns its id."""
    with store_session(store_id, "rw") as store:
        doc = Document(
            **{"filename": "a.md", "s3_key": "", "size_bytes": len(data), "sha256": "", **values}
        )
        store.add(doc)
        store.flush()
        doc.s3_key = files.document_key(store_id, doc.id)
        doc.sha256 = files.put(doc.s3_key, io.BytesIO(data), None).sha256
        store.commit()
        return doc.id


@pytest.fixture
def new_user(db) -> Iterator[Callable[[], User]]:
    """Makes users with no store yet; deletes them (and drops their stores) afterwards."""
    made: list[User] = []

    def make() -> User:
        u = sign_up(db, -(1000 + len(made)), f"new-user-{len(made)}")
        made.append(u)
        return u

    yield make
    for u in made:
        delete_user(db, u)


@pytest.fixture
def api(client, user, files_bucket):
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
