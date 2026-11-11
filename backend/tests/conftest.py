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
from sqlalchemy import select, text
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
from aios.models import DataStore, Organisation, OrganisationMember, User
from aios.passwords import hash_password
from aios.sandbox import runner
from aios.testing import fake_model

# The first segments of the API's unscoped paths (`aios.api.scoping`): what's the person's, not
# any one organisation's. `o` is the organisation prefix itself.
UNSCOPED = {"auth", "dev", "me", "invites", "notifications", "tools", "organisations", "o"}


def unscoped(path: str) -> bool:
    """Whether an API path is one of the unscoped routes', or already under the prefix."""
    rest = path.removeprefix("/api/")
    return rest.split("/", 1)[0].split("?", 1)[0] in UNSCOPED or rest == "triggers/timezones"


def home_organisation(db, user_id: int) -> Organisation | None:
    """The organisation tests act in for a user: the one they joined first (a test user's own,
    made with them by `make_user`), whatever else they've joined since; None if they're in
    none."""
    return db.scalar(
        select(Organisation)
        .join(OrganisationMember)
        .where(OrganisationMember.user_id == user_id)
        .order_by(OrganisationMember.joined_at, Organisation.id)
        .limit(1)
    )


class OrganisationClient(TestClient):
    """A client whose requests to an organisation's routes go under the organisation prefix, in
    the home organisation (`home_organisation`) of whoever is signed in: `get("/api/agents")`
    asks for `/api/o/<their organisation's id>/agents`, as the browser does in the organisation a
    page is open in. A path already under the prefix (to name another organisation) is sent as
    it is, and so is one of the unscoped routes'.

    Who's signed in is the user `current_user` is overridden with (the `api` fixture), else
    whoever `/api/me` says the client's session is; signed out, the path is sent as it is (no
    route answers it). Tests are about the routes' behaviour in an organisation, so most needn't
    name it; `test_organisation_prefix.py` is about the prefix itself."""

    def _organisation_id(self) -> int | None:
        override = app.dependency_overrides.get(current_user)
        if override is not None:
            user_id = override().id
        else:
            me = self.get("/api/me")
            if me.status_code != 200:
                return None
            user_id = me.json()["id"]
        with SessionLocal() as db:
            org = home_organisation(db, user_id)
            return None if org is None else org.id

    def request(self, method, url, *args, **kwargs):
        if isinstance(url, str) and url.startswith("/api/") and not unscoped(url):
            org_id = self._organisation_id()
            if org_id is not None:
                url = f"/api/o/{org_id}/{url.removeprefix('/api/')}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> TestClient:
    return OrganisationClient(app)


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
    """Delete every organisation the user is in (cascading to what each owns) with its store,
    the user, and the stores' databases if they were provisioned."""
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
    """A new user (as `build_user`) in an organisation of their own, named after them (with a
    random suffix if another organisation has that name), as creating one on the welcome page
    leaves them, with no store yet. Delete them with `delete_user`."""
    u = build_user(handle, **values)
    db.add(u)
    db.flush()
    name = u.full_name
    if db.scalar(select(Organisation.id).where(Organisation.name == name)) is not None:
        name = f"{name}-{secrets.token_hex(4)}"
    organisations.create(db, u, name)
    db.commit()
    return u


def store_of(user: User) -> uuid.UUID:
    """The id of the user's organisation's data store, made (not provisioned) if it has none."""
    with SessionLocal() as db:
        return data_store_for(home_organisation(db, user.id).id)


def user_store(user: User, role: str = "rw"):
    """A session on the user's organisation's store (`store_session`), where its agents, skills
    and workflows are; the store must be ready (`provisioned_user`)."""
    return store_session(store_of(user), role)


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
    """The user's organisation (`home_organisation`), for calling what takes one (as a request's
    would) directly."""
    return home_organisation(db, user.id)


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
    return OrganisationClient(app, cookies={settings.session_cookie: cookie.decode()})


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
