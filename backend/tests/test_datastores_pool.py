"""A request, or a task, holding one platform connection mustn't wait for a second: with enough of
them at once every connection in the pool is held by one that's waiting, and they all time out.
On a pool of one connection, anything that needs two at a time fails."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine

from aios import db as platform_db
from aios import search
from aios.api import documents
from aios.config import get_settings
from aios.datastores import connections, data_store_for
from aios.main import app
from aios.models import User


@pytest.fixture
def one_connection(db, user):
    """Platform sessions (the request's, the resolver's, store sessions) on a pool of one, which
    times out in a second rather than 30."""
    data_store_for(user)  # as on sign-in: the user has a store before any request
    small = create_engine(get_settings().database_url, pool_size=1, max_overflow=0, pool_timeout=1)
    platform_db.SessionLocal.configure(bind=small)
    connections._store_sessions.configure(bind=small)
    try:
        yield
    finally:
        platform_db.SessionLocal.configure(bind=platform_db.engine)
        connections._store_sessions.configure(bind=platform_db.engine)
        small.dispose()


@pytest.fixture
def signed_in(user):
    """A client signed in through the real session cookie, so `CurrentUser` loads the user with
    the request's own session."""
    settings = get_settings()
    cookie = TimestampSigner(settings.session_secret.get_secret_value()).sign(
        base64.b64encode(json.dumps({"user_id": user.id}).encode())
    )
    return TestClient(app, cookies={settings.session_cookie: cookie.decode()})


def test_document_endpoints_need_one_connection_at_a_time(one_connection, signed_in, monkeypatch):
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *args: None)

    async def embed_query(text):
        return [1.0] + [0.0] * 1023

    monkeypatch.setattr(search, "embed_query", embed_query)

    doc = signed_in.post("/api/documents", files={"file": ("a.txt", b"hello", "text/plain")})
    assert doc.status_code == 201
    assert [d["id"] for d in signed_in.get("/api/documents").json()] == [doc.json()["id"]]
    assert signed_in.get("/api/search", params={"q": "hello"}).status_code == 200
    assert signed_in.delete(f"/api/documents/{doc.json()['id']}").status_code == 204


def test_data_store_for_reads_with_the_connection_its_user_was_loaded_with(one_connection, user):
    # As a chat, an agent run or a workflow run does: the session that loaded the user is still
    # open, holding the pool's only connection.
    store_id = data_store_for(user).id
    with platform_db.SessionLocal() as session:
        loaded = session.get_one(User, user.id)
        assert data_store_for(loaded).id == store_id
        # The caller's session is left as it was, still usable.
        assert session.get_one(User, user.id).login == user.login
