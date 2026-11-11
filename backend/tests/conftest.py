import asyncio
import base64
import io
import json
import os
import secrets
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from psycopg import errors
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from aios import checkpoints, organisations, ratelimits
from aios.api import provenance
from aios.auth import current_user
from aios.config import get_settings
from aios.datastores import data_store_for, files, history, provision, resolve, store_session
from aios.datastores.connections import dispose_engines
from aios.datastores.models import Document
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import (
    Agent,
    DataStore,
    Organisation,
    OrganisationMember,
    Skill,
    User,
    Workflow,
)
from aios.passwords import hash_password
from aios.sandbox import runner
from aios.testing import fake_model


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
def rate_limits() -> None:
    """Every test starts with no attempts counted against the rate limits (in Redis)."""
    ratelimits.reset()


@pytest.fixture(autouse=True)
def store_deletions(monkeypatch) -> list[uuid.UUID]:
    """Data stores whose deletion sweep was queued (an empty organisation deleted on accepting
    an invitation), instead of sending it to the broker."""
    queued: list[uuid.UUID] = []
    monkeypatch.setattr(organisations, "enqueue_store_deletion", queued.append)
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
    """Delete the user's organisation (cascading to what it owns) with its store, the user, and
    the store's database if it was provisioned."""
    orgs = db.scalars(
        select(Organisation).join(OrganisationMember).where(OrganisationMember.user_id == user.id)
    ).all()
    owned = db.scalars(
        select(DataStore).where(DataStore.owner_organisation_id.in_([o.id for o in orgs]))
    ).all()
    stores = [provision.StoreNames.of(s) for s in owned]
    for store in owned:
        db.delete(store)
    for org in orgs:
        # What pins another's versions first (workflows pin agents', agents pin skills'): one
        # cascading delete of them all can check a pin before it has deleted the pinning row.
        for model in (Workflow, Agent, Skill):
            db.execute(delete(model).where(model.organisation_id == org.id))
        db.delete(org)
    db.delete(user)
    db.commit()
    if stores:
        dispose_engines()
        for names in stores:
            provision.drop(names)


# Every test user's password, unless a test sets another. Its hash is made once, here, so making
# users doesn't pay Argon2's cost each time.
TEST_PASSWORD = "correct horse battery staple"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


def build_user(handle: str | None = None, **values) -> User:
    """A user, not saved: every test's users are made here (or by `make_user`), so only this
    knows the user's columns. `handle` is a word that tells them apart in a test: their first
    name, and the start of their email (which a random part keeps unique). Their last name is
    "Tester", and their password `TEST_PASSWORD`. `values` sets other columns."""
    handle = handle or f"user{secrets.token_hex(4)}"
    return User(
        **{
            "first_name": handle,
            "last_name": "Tester",
            "email": f"{handle}-{secrets.token_hex(4)}@example.com",
            "password_hash": TEST_PASSWORD_HASH,
            **values,
        }
    )


def make_user(db, handle: str | None = None, **values) -> User:
    """A new user (as `build_user`) in their personal organisation, as registering leaves them,
    with no store yet. Delete them with `delete_user`."""
    u = build_user(handle, **values)
    db.add(u)
    db.flush()
    organisations.create_personal(db, u)
    db.commit()
    return u


def store_of(user: User) -> uuid.UUID:
    """The id of the user's organisation's data store, made (not provisioned) if it has none."""
    with SessionLocal() as db:
        return data_store_for(organisations.membership_of(db, user.id).id)


def join_organisation(db, of: User, handle: str | None = None, **values) -> User:
    """A new user (as `build_user`) who is a member of `of`'s organisation (not its creator).
    Delete them with `delete_user`, which deletes the shared organisation and what's in it."""
    u = build_user(handle, **values)
    db.add(u)
    db.flush()
    db.add(OrganisationMember(organisation_id=org_of(db, of).id, user_id=u.id))
    db.commit()
    return u


def org_of(db, user: User) -> Organisation:
    """The user's organisation, for calling what takes one (as a request's would) directly."""
    return organisations.membership_of(db, user.id)


def made_by(db, user: User) -> dict:
    """The columns that say whose a row (an agent, skill, workflow, trigger or chat) is, for a
    test that makes one directly as `user` would: their organisation's, made by them."""
    return provenance.made_by(org_of(db, user).id, user.id)


def provisioned_user(db, handle: str | None = None, **values) -> User:
    """A new user (as `make_user`), with their organisation's store provisioned: a database of
    its own, ready. Delete them with `delete_user`."""
    u = make_user(db, handle, **values)
    provision.provision(store_of(u))
    return u


def signed_in_client(user: User) -> TestClient:
    """A client signed in as `user` through a real session cookie, as `auth.start_session`
    leaves it, so `CurrentUser` loads the user with the request's own session."""
    settings = get_settings()
    data = {"user_id": user.id, "session_version": user.session_version}
    session = base64.b64encode(json.dumps(data).encode())
    cookie = TimestampSigner(settings.session_secret.get_secret_value()).sign(session)
    return TestClient(app, cookies={settings.session_cookie: cookie.decode()})


def register(client: TestClient, first_name: str, last_name: str = "Tester", **values):
    """Register a new account through the API, as the sign-up page does: a fresh email and the
    password `TEST_PASSWORD`; `values` adds to the body (an invitation link's `invite`). Answers
    the response. Delete the user it made with `delete_user`."""
    body = {
        "first_name": first_name,
        "last_name": last_name,
        "email": f"registered-{secrets.token_hex(4)}@example.com",
        "password": TEST_PASSWORD,
        "password_confirm": TEST_PASSWORD,
        **values,
    }
    return client.post("/api/auth/register", json=body)


@pytest.fixture
def user(db):
    """A user whose store is provisioned and ready."""
    u = provisioned_user(db, "test-user", first_name="Test", last_name="User")
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
    """Makes users (each in an organisation of their own) with no store yet; deletes them (and
    drops their stores) afterwards."""
    made: list[User] = []

    def make() -> User:
        u = make_user(db, f"new-user-{len(made)}")
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
