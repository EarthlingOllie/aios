import asyncio
import base64
import io
import json
import os
import secrets
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from langchain_core.messages import HumanMessage
from psycopg import errors
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from aios import jev, organisations, ratelimits, turn_lease
from aios import run_service as run_service_package
from aios.api import provenance
from aios.auth import current_user
from aios.config import get_settings, libpq_conninfo
from aios.datastores import credentials, data_store_for, files, provision, resolve, store_session
from aios.datastores.connections import dispose_engines
from aios.datastores.models import Document
from aios.db import SessionLocal, engine
from aios.main import app
from aios.models import AgentStoreRole, DataStore, Organisation, OrganisationMember, User
from aios.passwords import hash_password
from aios.run_service import executor
from aios.sandbox import runner
from aios.sources import uploads_source_id
from aios.testing import fake_model

# The first segments of the API's unscoped paths (`aios.api.scoping`): what's the person's, not
# any one organisation's. `o` is the organisation prefix itself.
UNSCOPED = {"auth", "dev", "me", "invites", "tools", "organisations", "o", "webhooks"}


def unscoped(path: str) -> bool:
    """Whether an API path is one of the unscoped routes', or already under the prefix."""
    rest = path.removeprefix("/api/")
    return (
        rest.split("/", 1)[0].split("?", 1)[0] in UNSCOPED
        or rest == "triggers/timezones"
        or rest.split("?", 1)[0] == "connectors/callback"
    )


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
    with SessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture(autouse=True)
def provisioning(monkeypatch) -> list[uuid.UUID]:
    """Data stores whose provisioning was queued, instead of sending it to the broker."""
    queued: list[uuid.UUID] = []
    monkeypatch.setattr(resolve, "enqueue_provisioning", queued.append)
    return queued


class AgentAccess:
    """Applying agents' logins and grants (`aios.datastores.agent_roles`), as the urgent lane
    would: at once when queued, as if its worker were always idle. With `auto` off, stores wait
    in `queued` until the test applies them (`apply`). As the task's failure would, one that
    fails (a store only marked ready, with no database) fails nothing but itself: it's kept in
    `failed`."""

    def __init__(self) -> None:
        self.auto = True
        self.queued: list[uuid.UUID] = []
        self.failed: list[BaseException] = []

    def enqueue(self, data_store_id: uuid.UUID) -> None:
        from aios.datastores import agent_roles

        if not self.auto:
            self.queued.append(data_store_id)
            return
        try:
            agent_roles.apply(data_store_id)
        except Exception as exc:  # noqa: BLE001 - the task's failure, not the caller's
            self.failed.append(exc)

    def apply(self) -> None:
        from aios.datastores import agent_roles

        while self.queued:
            agent_roles.apply(self.queued.pop(0))


@pytest.fixture(autouse=True)
def agent_access(monkeypatch) -> AgentAccess:
    from aios.datastores import agent_roles

    access = AgentAccess()
    monkeypatch.setattr(agent_roles, "enqueue", access.enqueue)
    return access


@pytest.fixture(autouse=True)
def rate_limits() -> None:
    """Every test starts with no attempts counted against the rate limits (in Redis)."""
    ratelimits.reset()


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class RunService:
    """The run service as tests run it (`aios.run_service`): whatever a request wakes it for is
    run in-process once the response is sent, by its drain-once entry point, as if a run
    service process were always up. With `auto` off, work waits in the queue until the test
    drains it (`drain`)."""

    def __init__(self) -> None:
        self.auto = True
        self._draining = threading.local()

    def drain(self, **kwargs) -> None:
        """Run every ready item to completion, here and now."""
        asyncio.run(executor.drain_once(**kwargs))

    def woken(self, *lanes) -> None:
        # Not within a drain (a request made from inside a turn, or an item woken from its
        # event loop, `start_workflow`'s): that drain picks it up.
        if not self.auto or getattr(self._draining, "on", False) or _in_event_loop():
            return
        self._draining.on = True
        try:
            self.drain()
        finally:
            self._draining.on = False


@pytest.fixture(autouse=True)
def run_service(monkeypatch) -> RunService:
    """The run service (see `RunService`), with an empty queue: tests share the database, and
    each starts with none of another's work."""
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM work_items"))
    except (OperationalError, ProgrammingError):
        pass
    service = RunService()
    monkeypatch.setattr(run_service_package, "wake", service.woken)
    return service


class TurnStream:
    """What a turn of a run streamed (`take_turn`): its chunks, and as the server-sent events
    they'd be sent as (`text`)."""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.text = "".join(
            f"data: {json.dumps(c, separators=(',', ':'), default=str)}\n\n" for c in chunks
        )

    def text_deltas(self) -> str:
        return "".join(c["delta"] for c in self.chunks if c["type"] == "text-delta")

    @property
    def error(self) -> str | None:
        return next((c["errorText"] for c in self.chunks if c["type"] == "error"), None)


async def run_turn(store_id: uuid.UUID, agent_id: int, run_id: int, text: str) -> TurnStream:
    """One turn of the store's run with `text` as the user's message, run here as the run
    service runs a turn (`aios.run_service.work`): under the run's turn lease, through the turn
    code every turn shares (`aios.api.agents.turn_stream`). Run pages are read-only, and people
    talk to agents by mentioning them in chats; this is how tests give an existing run another
    turn. Raises `HTTPException` as loading the run would (404 if it's not the agent's)."""
    from aios.api import agents as agents_api

    with SessionLocal() as db:
        ctx = await asyncio.to_thread(agents_api.load_chat_context, db, store_id, agent_id, run_id)
    lease = await turn_lease.acquire(store_id, run_id)
    try:
        turn_id = await asyncio.to_thread(agents_api._start_turn, store_id, run_id, text)
    except BaseException:
        await turn_lease.release(lease)
        raise
    message = HumanMessage(text, id=str(uuid.uuid4()))
    chunks = agents_api.turn_stream(ctx, run_id, message, lease, turn_id=turn_id)
    return TurnStream([chunk async for chunk in chunks])


def take_turn(api, agent_id: int, run_id: int, text: str) -> TurnStream:
    """`run_turn` in the store of the organisation `api` acts in, here and now."""
    return asyncio.run(run_turn(signed_in_store(), agent_id, run_id, text))


def enqueue_advance(store_id: uuid.UUID, run_id: int) -> None:
    """Enqueue an advance of the store's workflow run (`aios.workflows.advance`), as starting it
    or a decision does: a no-op while one is queued."""
    from aios import workflows

    with SessionLocal() as db:
        organisation_id = db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == store_id)
        )
        run_service_package.enqueue(db, [workflows.advance_work(organisation_id, store_id, run_id)])
        db.commit()


async def run_workflow(store_id: uuid.UUID, run_id: int) -> None:
    """Advance the store's workflow run on the run service, by its drain-once entry point, and
    run everything that enqueues, until nothing ready is left: to the end, or until it waits."""
    await asyncio.to_thread(enqueue_advance, store_id, run_id)
    await executor.drain_once()


def drain_run(store_id: uuid.UUID, run_id: int) -> None:
    """`run_workflow`, here and now."""
    asyncio.run(run_workflow(store_id, run_id))


class QueuedRuns(list):
    """The ids of the workflow runs the API (or a schedule, or a mention) enqueued, and the
    store they're in (`store`)."""

    def __init__(self, store: uuid.UUID | None) -> None:
        super().__init__()
        self.store = store


def capture_runs(monkeypatch, store: uuid.UUID | None) -> QueuedRuns:
    """Record each workflow run started, instead of enqueueing its first advance: the test runs
    it when it chooses (`drain_run`), as the run service would."""
    from aios.api import workflows as workflows_api

    ids = QueuedRuns(store)

    def recording(work) -> None:
        if ids.store is None:
            ids.store = work.data_store_id
        assert work.data_store_id == ids.store
        ids.append(work.payload["run_id"])

    monkeypatch.setattr(workflows_api, "enqueue_run", recording)
    return ids


class ResumeTask:
    """Stands in, in a test's list of queued tasks, for carrying on the run that made a decided
    request (`aios.run_service.work.enqueue_resume`): run, it enqueues that on the run service
    and drains the queue, as the run service would."""

    def __init__(self) -> None:
        from aios.run_service import work

        self.enqueue = work.enqueue_resume

    def run(self, data_store_id, request_id) -> None:
        from aios.run_service import work

        resume = work.resume_for(uuid.UUID(str(data_store_id)), uuid.UUID(str(request_id)))
        if resume is not None:
            with SessionLocal() as db:
                run_service_package.enqueue(db, [resume])
                db.commit()
        asyncio.run(executor.drain_once())


RESUME = ResumeTask()


def capture_resumes(monkeypatch, tasks: list) -> None:
    """Record each resume enqueued, as `(RESUME, (store id, request id))`, instead of enqueueing
    it: the test runs it when it chooses."""
    from aios.run_service import work

    monkeypatch.setattr(work, "enqueue_resume", lambda *args: tasks.append((RESUME, args)))


@pytest.fixture(autouse=True)
def jev_classifier() -> Iterator[jev.FakeJev]:
    """Jev (`aios.jev`), as its fake: flags the commonest prompt injections, and a test can take
    it `down`, or see what it was sent."""
    with jev.use(jev.FakeJev()) as fake:
        yield fake


@pytest.fixture(autouse=True)
def handoff_model() -> Iterator[None]:
    """The handoff step's model (`aios.handoff`), scripted rather than the provider's: it
    acknowledges with "On it." and briefs nothing (`fake_model.use_handoff` scripts another).
    Not with `monkeypatch`, which a test may undo."""
    with fake_model.use_handoff(fake_model.briefs()):
        yield


@pytest.fixture(autouse=True)
def handback_model() -> Iterator[None]:
    """The handback step's model (`aios.handback`), scripted rather than the provider's: its
    summary is the output it was sent, as it came (`fake_model.use_handback` scripts another)."""
    with fake_model.use_handback(fake_model.summarises()):
        yield


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


def signed_in_store() -> uuid.UUID:
    """The id of the store of the organisation the `api` fixture's user acts in (whoever
    `current_user` is overridden with now)."""
    return store_of(app.dependency_overrides[current_user]())


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


class Rows(list):
    """Rows read from a store, as a session's results are used."""

    def all(self) -> list:
        return list(self)

    def one(self):
        (row,) = self
        return row

    def one_or_none(self):
        return self.one() if self else None

    def first(self):
        return self[0] if self else None


class StoreRows:
    """Reads and writes on one store, each in a session of its own (so it never sees stale
    rows), as tests once did on the platform database's session: where runs are now."""

    def __init__(self, store_id: uuid.UUID) -> None:
        self.store_id = store_id

    def get(self, model, ident):
        with store_session(self.store_id) as store:
            return store.get(model, ident)

    def scalars(self, statement) -> Rows:
        with store_session(self.store_id) as store:
            return Rows(store.scalars(statement).all())

    def scalar(self, statement):
        with store_session(self.store_id) as store:
            return store.scalar(statement)

    def rows(self, statement) -> Rows:
        with store_session(self.store_id) as store:
            return Rows(store.execute(statement).all())

    def execute(self, statement) -> None:
        """Run a write, and commit it."""
        with store_session(self.store_id) as store:
            store.execute(statement)
            store.commit()

    def add(self, *rows):
        """Add these rows, and commit them; returns the first, ids and all."""
        with store_session(self.store_id) as store:
            store.add_all(rows)
            store.commit()
        return rows[0]


def rows_of(user: User) -> StoreRows:
    """Reads and writes on the user's organisation's store (`StoreRows`)."""
    return StoreRows(store_of(user))


@pytest.fixture
def in_store(user) -> StoreRows:
    """Reads and writes on the signed-in user's store (`StoreRows`), where runs are."""
    return rows_of(user)


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
            **{
                "filename": "a.md",
                "s3_key": "",
                "size_bytes": len(data),
                "sha256": "",
                "source_id": uploads_source_id(store),
                **values,
            }
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


# --- Agents' own logins (`aios.datastores.agent_roles`) ------------------------------------------


def role_row(store: DataStore, agent: dict) -> AgentStoreRole | None:
    with SessionLocal() as db:
        return db.scalar(
            select(AgentStoreRole).where(
                AgentStoreRole.data_store_id == store.id, AgentStoreRole.agent_id == agent["id"]
            )
        )


def login(store: DataStore, agent: dict, database: str | None = None) -> str:
    """libpq connection info for the agent's own login, straight to Postgres."""
    row = role_row(store, agent)
    url = get_settings().database_url
    return libpq_conninfo(
        url,
        database=database or store.dbname,
        username=row.role_name,
        password=credentials.decrypt(row.password),
    )


def as_agent(store: DataStore, agent: dict, database: str | None = None) -> psycopg.Connection:
    return psycopg.connect(login(store, agent, database), autocommit=True)
