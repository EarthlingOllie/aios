import ast
import asyncio
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from celery.exceptions import Retry
from conftest import add_document, delete_user, provisioned_user
from fake_model import ToolCall, last_tool_results, model, use_model
from psycopg import sql
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from test_agents import agent_body, chat_body, make_agent
from test_workflows import agent_node, make_workflow

import aios
from aios import agent as agent_module
from aios import auth, ingest, search, worker, workflows
from aios.api import agents as agents_api
from aios.api import documents
from aios.api import workflows as workflows_api
from aios.config import get_settings, libpq_conninfo
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    data_store_for,
    provision,
    store_engine,
    store_session,
)
from aios.datastores import connections as store_connections
from aios.datastores.models import EMBEDDING_DIM, Document, DocumentStatus
from aios.db import SessionLocal, engine
from aios.models import (
    Base,
    DataStore,
    DataStoreStatus,
    User,
    WorkflowRun,
    WorkflowRunStatus,
)
from aios.tools import hybrid_search
from aios.ui_stream import error_text
from aios.workflows import execute_run


def test_a_user_becomes_a_store_of_its_own(db, user):
    store_id = data_store_for(user.id)

    assert isinstance(store_id, uuid.UUID)
    # The same store every time.
    assert data_store_for(user.id) == store_id

    other = User(github_id=-2, login="other-user")
    db.add(other)
    db.commit()
    try:
        assert data_store_for(other.id) != store_id
    finally:
        db.delete(other)
        db.commit()


def test_a_session_is_opened_only_on_a_store_that_exists_and_is_ready(db, user):
    """(Sessions on a provisioned store's own database: see test_provisioning.)"""
    store_id = data_store_for(user.id)
    with store_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT 1")) == 1

    with pytest.raises(DataStoreNotFound), store_session(uuid.uuid4(), "rw"):
        pass
    with pytest.raises(ValueError, match="role"), store_session(store_id, "admin"):
        pass
    db.execute(
        update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.failed)
    )
    db.commit()
    with pytest.raises(DataStoreNotReady), store_session(store_id, "ro"):
        pass


@pytest.fixture
def unready_user(db, user) -> User:
    """`user`, with a store that's still being provisioned."""
    db.execute(
        update(DataStore)
        .where(DataStore.owner_user_id == user.id)
        .values(status=DataStoreStatus.provisioning)
    )
    db.commit()
    return user


def test_documents_and_search_answer_409_until_the_store_is_ready(api, unready_user, monkeypatch):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    responses = [
        api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")}),
        api.get("/api/documents"),
        api.get("/api/search", params={"q": "fish"}),
    ]

    for r in responses:
        assert r.status_code == 409, r.text
        assert r.json() == {
            "detail": "Your data store is being set up. Try again in a moment.",
            "data_store_status": "provisioning",
        }
    assert queued == []
    assert api.get("/api/data-store").json()["status"] == "provisioning"


def test_a_store_that_failed_to_provision_says_so(api, db, unready_user):
    db.execute(
        update(DataStore)
        .where(DataStore.owner_user_id == unready_user.id)
        .values(status=DataStoreStatus.failed, error="boom")
    )
    db.commit()

    r = api.get("/api/documents")

    assert r.status_code == 409
    assert r.json()["data_store_status"] == "failed"
    assert "boom" not in r.text
    assert api.get("/api/data-store").json()["status"] == "failed"


def search_tool_results(api) -> list:
    """What the model sees from one `search_documents` call in a chat."""
    body = chat_body("fish?")
    body["messages"][0]["role"] = "user"
    seen = []

    async def search_then_answer_seeing(messages, info):
        if results := last_tool_results(messages):
            seen.extend(results)
            yield "done"
        else:
            yield ToolCall("search_documents", {"query": "fish"})

    with use_model(model(search_then_answer_seeing)):
        assert api.post("/api/chat", json=body).status_code == 200
    return seen


def test_the_search_tools_tell_the_model_the_store_is_being_set_up(api, unready_user):
    seen = search_tool_results(api)

    assert seen and all("still being set up" in str(r) for r in seen)


def test_the_search_tools_tell_the_model_the_store_couldnt_be_set_up(api, db, unready_user):
    """Not "being set up", which would have the user wait for what may never come."""
    db.execute(
        update(DataStore)
        .where(DataStore.owner_user_id == unready_user.id)
        .values(status=DataStoreStatus.failed)
    )
    db.commit()

    seen = search_tool_results(api)

    assert seen and all("couldn't be set up" in r.content for r in seen)


def test_the_search_tools_tell_the_model_a_store_behind_the_trees_head_is_being_upgraded(
    db, user, monkeypatch
):
    store_id = data_store_for(user.id)
    db.execute(update(DataStore).where(DataStore.id == store_id).values(schema_revision="0001"))
    db.commit()

    async def embed(query):
        return [0.0] * EMBEDDING_DIM

    monkeypatch.setattr(search, "embed_query", embed)

    results = asyncio.run(search.document_search_results(data_store_id=store_id, query="fish"))

    assert results == [{"error": search.STORE_UPGRADING}]


def test_an_upload_is_ingested_in_the_users_store(api, db, user, monkeypatch):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    doc = api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")}).json()

    assert queued == [(str(data_store_for(user.id)), doc["id"])]


@pytest.fixture
def offline_ingest(monkeypatch, files_bucket):
    def chunk_document(filename, data):
        return [{"text": "Hi", "contextualized": "Hi", "meta": {}}]

    async def embed_documents(texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(ingest, "chunk_document", chunk_document)
    monkeypatch.setattr(ingest, "embed_documents", embed_documents)


def stored_document(store_id: uuid.UUID, document_id: int) -> Document | None:
    """The document, with its chunks, from the store's own database."""
    with store_session(store_id, "ro") as store:
        doc = store.get(Document, document_id)
        if doc is not None:
            doc.chunks  # noqa: B018 - loaded while the session is open
        return doc


def test_an_upload_is_ingested_and_found_by_search_in_the_users_store(
    api, user, offline_ingest, monkeypatch
):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    async def embed_query(text):
        return [1.0] + [0.0] * 1023

    monkeypatch.setattr(search, "embed_query", embed_query)

    upload = api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")})
    assert upload.status_code == 201, upload.text
    for args in queued:
        worker.ingest_document_task.run(*args)

    [listed] = api.get("/api/documents").json()
    assert (listed["filename"], listed["status"], listed["chunk_count"]) == ("a.md", "ready", 1)
    hits = api.get("/api/search", params={"q": "hi"}).json()
    assert [(h["filename"], h["text"]) for h in hits] == [("a.md", "Hi")]
    # In the store's own database.
    stored = stored_document(data_store_for(user.id), upload.json()["id"])
    assert [c.text for c in stored.chunks] == ["Hi"]

    assert api.delete(f"/api/documents/{listed['id']}").status_code == 204
    assert api.get("/api/documents").json() == []
    assert api.get("/api/search", params={"q": "hi"}).json() == []


def test_the_ingest_task_ingests_in_the_store_it_is_given(user, offline_ingest):
    store_id = data_store_for(user.id)
    doc_id = add_document(store_id)

    worker.ingest_document_task.run(str(store_id), doc_id)

    doc = stored_document(store_id, doc_id)
    assert doc.status == DocumentStatus.ready
    assert [c.text for c in doc.chunks] == ["Hi"]


def test_the_ingest_task_skips_a_store_deleted_since(db, offline_ingest):
    """As for a document deleted before its ingest: there's nothing left to ingest."""
    owner = provisioned_user(db, -4, "gone")
    store_id = data_store_for(owner.id)
    doc_id = add_document(store_id)
    delete_user(db, owner)  # and, with them, their store

    worker.ingest_document_task.run(str(store_id), doc_id)


def test_the_ingest_task_skips_a_store_being_deleted(db, user, offline_ingest):
    """A store stops being ready only once it's being deleted (uploads wait until it's ready),
    and its documents go with it."""
    store_id = data_store_for(user.id)
    doc_id = add_document(store_id)
    db.execute(
        update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.deleting)
    )
    db.commit()

    worker.ingest_document_task.run(str(store_id), doc_id)


@pytest.fixture
def searches(monkeypatch) -> list[dict]:
    """What the document search tools search (the documents chat's and the agents' tool)."""
    calls = []

    async def results(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(agent_module, "document_search_results", results)
    monkeypatch.setattr(hybrid_search, "document_search_results", results)
    return calls


async def search_then_answer(messages, info):
    if last_tool_results(messages):
        yield "done"
    else:
        yield ToolCall("search_documents", {"query": "fish"})


def librarian(api) -> dict:
    agent = make_agent(api, "Librarian")
    tools = [{"key": "hybrid_search", "config": {"max_results": 3}}]
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()


def test_the_documents_chat_searches_the_users_store(api, user, searches):
    body = chat_body("fish?")
    body["messages"][0]["role"] = "user"
    with use_model(model(search_then_answer)):
        assert api.post("/api/chat", json=body).status_code == 200

    assert searches == [{"data_store_id": data_store_for(user.id), "query": "fish"}]


def test_an_agent_run_searches_the_users_store(api, user, searches):
    agent = librarian(api)
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(model(search_then_answer)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("fish?")).status_code == 200

    assert searches == [{"data_store_id": data_store_for(user.id), "query": "fish", "limit": 3}]


def test_a_workflow_run_searches_its_owners_store(api, user, searches, monkeypatch):
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", lambda run_id: None)
    agent = librarian(api)
    workflow = make_workflow(api, "Library", [agent_node("look", agent)])
    started = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "fish?"}).json()
    with use_model(model(search_then_answer)):
        asyncio.run(execute_run(started["id"]))

    assert searches == [{"data_store_id": data_store_for(user.id), "query": "fish", "limit": 3}]


def test_a_workflow_run_whose_store_cant_be_resolved_is_failed_unstarted(api, db, monkeypatch):
    """Resolving the store is part of claiming the run, so if it fails the run is never left
    `running` with no worker (a redelivery could never start it), nor `queued` with nothing to
    run it: it's failed then and there, with the real error."""
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", lambda run_id: None)
    workflow = make_workflow(api, "Library", [agent_node("look", librarian(api))])
    started = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "fish?"}).json()

    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(workflows, "data_store_for", unavailable)
    asyncio.run(execute_run(started["id"]))

    run = db.get(WorkflowRun, started["id"])
    assert (run.status, run.started_at) == (WorkflowRunStatus.failed, None)
    assert run.error == error_text(OperationalError("SELECT", {}, Exception("connection lost")))


@contextmanager
def connections() -> Iterator[dict]:
    """Counts the most pooled connections checked out at once inside the block (`most`)."""
    out = {"now": 0, "most": 0}

    def checkout(*args):
        out["now"] += 1
        out["most"] = max(out["most"], out["now"])

    def checkin(*args):
        out["now"] -= 1

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        yield out
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_loading_a_turn_resolves_its_store_on_the_turns_connection(api, user):
    """A turn's context is loaded on the request's session, without taking a second pooled
    connection while it holds the first (a burst of turns could otherwise exhaust the pool)."""
    agent = librarian(api)
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with connections() as used, SessionLocal() as session:
        ctx = agents_api.load_chat_context(session, user.id, agent["id"], run["id"])

    assert ctx.data_store_id == data_store_for(user.id)
    assert used["most"] == 1


SRC = Path(aios.__file__).parent
# How a module opens a platform database session.
PLATFORM_SESSIONS = {"SessionLocal", "get_db", "DB", "engine"}


def touches_user_data(tree: ast.Module, source: str) -> bool:
    """Whether a module uses the documents or chunks tables, by model or in SQL."""
    models = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aios.datastores.models"
        for alias in node.names
    }
    return bool(models & {"Document", "Chunk"}) or re.search(
        r"\b(FROM|JOIN)\s+(aios\.)?(documents|chunks)\b", source, re.IGNORECASE
    )


def platform_sessions(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aios.db"
        for alias in node.names
    } & PLATFORM_SESSIONS


def test_the_seam_check_sees_the_modules_that_use_user_data():
    users = set()
    for path in SRC.rglob("*.py"):
        source = path.read_text()
        if touches_user_data(ast.parse(source), source):
            users.add(str(path.relative_to(SRC)))
    assert {"search.py", "ingest.py", "api/documents.py"} <= users


def test_only_the_seam_opens_sessions_on_user_data():
    offenders = {}
    for path in SRC.rglob("*.py"):
        module = path.relative_to(SRC)
        if module.parts[0] == "datastores" or module.name == "models.py":
            continue
        source = path.read_text()
        tree = ast.parse(source)
        if touches_user_data(tree, source) and (opened := platform_sessions(tree)):
            offenders[str(module)] = sorted(opened)
    assert offenders == {}


def test_the_platform_database_has_no_documents_or_chunks(db):
    assert not {"documents", "chunks"} & set(Base.metadata.tables)
    platform = db.scalars(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).all()
    assert "data_stores" in platform
    assert not {"documents", "chunks"} & set(platform)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_signing_in_gives_the_user_a_store_and_queues_its_provisioning(
    client, db, monkeypatch, provisioning
):
    github = auth.oauth.github

    async def authorize_access_token(request):
        return {"access_token": "t"}

    async def get(path, token):
        assert path == "user"
        return FakeResponse({"id": -3, "login": "new-user", "email": "new@example.com"})

    monkeypatch.setattr(github, "authorize_access_token", authorize_access_token)
    monkeypatch.setattr(github, "get", get)

    resp = client.get("/auth/callback?code=x&state=y", follow_redirects=False)

    assert resp.status_code == 303
    user = db.scalar(select(User).where(User.github_id == -3))
    try:
        stores = db.scalars(select(DataStore).where(DataStore.owner_user_id == user.id)).all()
        assert [s.status for s in stores] == [DataStoreStatus.provisioning]
        assert provisioning == [stores[0].id]
    finally:
        db.delete(user)
        db.commit()


def test_a_store_created_in_a_callers_transaction_is_queued_once_committed(db, provisioning):
    owner = User(github_id=-5, login="later")
    db.add(owner)
    db.commit()
    try:
        store_id = data_store_for(owner.id, db)
        assert provisioning == []
        db.commit()
        assert provisioning == [store_id]
        # Not again for a store that exists.
        assert data_store_for(owner.id, db) == store_id
        db.commit()
        assert provisioning == [store_id]
    finally:
        db.delete(owner)
        db.commit()


def test_a_store_rolled_back_with_its_callers_transaction_is_never_queued(db, provisioning):
    owner = User(github_id=-6, login="undone")
    db.add(owner)
    db.commit()
    try:
        data_store_for(owner.id, db)
        db.rollback()
        db.commit()
        assert provisioning == []
    finally:
        db.delete(owner)
        db.commit()


@pytest.fixture
def busy_store(db, user) -> Iterator[uuid.UUID]:
    """`user`'s store, with its roles holding every connection they may."""
    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
    names = provision.StoreNames.of(store)
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as provisioner:
        for role in (names.rw, names.ro):
            provisioner.execute(
                sql.SQL("ALTER ROLE {} CONNECTION LIMIT 1").format(sql.Identifier(role))
            )
    with store_engine(store.id, "rw").connect(), store_engine(store.id, "ro").connect():
        yield store.id


def test_a_busy_store_answers_503_to_try_again(api, busy_store, monkeypatch):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    r = api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")})

    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Your data store is busy. Try again in a moment."}
    assert r.headers["Retry-After"] == "1"
    assert queued == []
    with pytest.raises(DataStoreBusy), store_session(busy_store, "rw") as store:
        store.execute(text("SELECT 1"))


def test_the_ingest_task_tries_a_busy_store_again(busy_store, monkeypatch):
    retried = []

    def retry(**kwargs):
        retried.append(kwargs)
        return Retry()

    monkeypatch.setattr(worker.ingest_document_task, "retry", retry)

    with pytest.raises(Retry):
        worker.ingest_document_task.run(str(busy_store), 1)

    assert retried == [{"countdown": worker.BUSY_RETRY_COUNTDOWN}]


def test_the_ingest_task_never_gives_up_on_a_busy_store(busy_store, monkeypatch):
    # Giving up would leave the document pending with nothing left to ingest it, and marking it
    # failed needs the very connection the store hasn't got free.
    queued = []
    monkeypatch.setattr(
        worker.ingest_document_task, "apply_async", lambda *a, **kw: queued.append(kw)
    )
    worker.ingest_document_task.push_request(id="busy", retries=1000, called_directly=False)
    try:
        with pytest.raises(Retry):
            worker.ingest_document_task.run(str(busy_store), 1)
    finally:
        worker.ingest_document_task.pop_request()

    assert [kw["countdown"] for kw in queued] == [worker.BUSY_RETRY_COUNTDOWN]


def test_a_store_busy_mid_ingest_leaves_the_document_for_the_retry(
    user, offline_ingest, monkeypatch
):
    """Busy once the document is claimed (its connection went back to the pool through the
    conversion, and the store's are all in use by the time it's wanted again): the retry, not
    the failure, is what's recorded."""
    store_id = data_store_for(user.id)
    doc_id = add_document(store_id)
    monkeypatch.setattr(worker.ingest_document_task, "retry", lambda **kwargs: Retry())

    busy = [True]

    def busy_once(conn, cursor, statement, *args):
        if statement.startswith("DELETE FROM aios.chunks") and busy and busy.pop():
            raise DataStoreBusy(store_id)

    rw = store_engine(store_id, "rw")
    event.listen(rw, "before_cursor_execute", busy_once)
    try:
        with pytest.raises(Retry):
            worker.ingest_document_task.run(str(store_id), doc_id)
    finally:
        event.remove(rw, "before_cursor_execute", busy_once)

    assert stored_document(store_id, doc_id).status == DocumentStatus.processing
    worker.ingest_document_task.run(str(store_id), doc_id)  # the retry
    doc = stored_document(store_id, doc_id)
    assert doc.status == DocumentStatus.ready
    assert [c.text for c in doc.chunks] == ["Hi"]


def test_the_search_tools_tell_the_model_the_store_is_busy(busy_store, monkeypatch):
    async def embed_query(text):
        return [1.0] + [0.0] * 1023

    monkeypatch.setattr(search, "embed_query", embed_query)
    results = asyncio.run(search.document_search_results(data_store_id=busy_store, query="fish"))

    assert results == [{"error": search.STORE_BUSY}]


def test_a_store_whose_pool_in_this_process_is_full_answers_503(api, user, monkeypatch):
    # Below the role's connection limit, but every connection this process pools on the store
    # is in use (polling, an upload and searches at once, say).
    monkeypatch.setattr(store_connections, "POOL_TIMEOUT", 0.1)
    store_connections.dispose_engines()
    ro = store_engine(data_store_for(user.id), "ro")
    with ro.connect(), ro.connect(), ro.connect():
        r = api.get("/api/documents")

    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Your data store is busy. Try again in a moment."}


@pytest.mark.parametrize(
    "refusal",
    [
        "sorry, too many clients already",
        "remaining connection slots are reserved for roles with the SUPERUSER attribute",
    ],
)
def test_a_store_on_an_instance_at_its_connection_limit_is_busy(user, refusal, monkeypatch):
    """The instance's `max_connections` taken, rather than the store role's limit: as
    transient, and as worth retrying. (Postgres's own words, from a server at its limit.)"""
    store_connections.dispose_engines()
    ro = store_engine(data_store_for(user.id), "ro")

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError(
            'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
            f"FATAL:  {refusal}"
        )

    event.listen(ro, "do_connect", refuse)
    try:
        with pytest.raises(DataStoreBusy), ro.connect():
            pass
    finally:
        store_connections.dispose_engines()  # The engine that refuses goes with it.
