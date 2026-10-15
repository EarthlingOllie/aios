import ast
import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fake_model import ToolCall, last_tool_results, model, use_model
from sqlalchemy import select, text
from test_agents import agent_body, chat_body, make_agent
from test_workflows import agent_node, make_workflow

import aios
from aios import agent as agent_module
from aios import auth, ingest, worker
from aios.api import documents
from aios.api import workflows as workflows_api
from aios.datastores import DataStoreNotFound, data_store_for, store_session
from aios.models import DataStore, Document, DocumentStatus, User
from aios.tools import hybrid_search
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


def test_a_session_is_opened_on_a_store(db, user):
    store_id = data_store_for(user.id)
    with store_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT 1")) == 1

    with pytest.raises(DataStoreNotFound), store_session(uuid.uuid4(), "rw"):
        pass
    with pytest.raises(ValueError, match="role"), store_session(store_id, "admin"):
        pass


def test_an_upload_is_ingested_in_the_users_store(api, db, user, monkeypatch):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    doc = api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")}).json()

    assert queued == [(str(data_store_for(user.id)), doc["id"])]


def test_the_ingest_task_ingests_in_the_store_it_is_given(db, user, monkeypatch):
    def chunk_document(filename, data):
        return [{"text": "Hi", "contextualized": "Hi", "meta": {}}]

    async def embed_documents(texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(ingest, "chunk_document", chunk_document)
    monkeypatch.setattr(ingest, "embed_documents", embed_documents)
    doc = Document(owner_id=user.id, filename="a.md", data=b"# Hi")
    db.add(doc)
    db.commit()

    with pytest.raises(DataStoreNotFound):
        worker.ingest_document_task.run(str(uuid.uuid4()), doc.id)
    worker.ingest_document_task.run(str(data_store_for(user.id)), doc.id)

    db.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert [c.text for c in doc.chunks] == ["Hi"]


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


SRC = Path(aios.__file__).parent
# How a module opens a platform database session.
PLATFORM_SESSIONS = {"SessionLocal", "get_db", "DB", "engine"}


def touches_user_data(tree: ast.Module, source: str) -> bool:
    """Whether a module uses the documents or chunks tables, by model or in SQL."""
    models = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aios.models"
        for alias in node.names
    }
    return bool(models & {"Document", "Chunk"}) or re.search(
        r"\b(FROM|JOIN)\s+(documents|chunks)\b", source, re.IGNORECASE
    )


def platform_sessions(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aios.db"
        for alias in node.names
    } & PLATFORM_SESSIONS


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


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_signing_in_gives_the_user_a_store(client, db, monkeypatch):
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
        assert len(stores) == 1
    finally:
        db.delete(user)
        db.commit()
