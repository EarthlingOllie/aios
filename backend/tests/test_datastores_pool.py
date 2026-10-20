"""A request, or a task, holding one platform connection mustn't wait for a second: with enough of
them at once every connection in the pool is held by one that's waiting, and they all time out.
On a pool of one connection, anything that needs two at a time fails."""

import pytest
from conftest import signed_in_client
from sqlalchemy import create_engine

from aios import db as platform_db
from aios import search
from aios.api import documents
from aios.config import get_settings


@pytest.fixture
def one_connection(db, user):
    """Platform sessions (the request's, the resolver's, the documents') on a pool of one, which
    times out in a second rather than 30. (`user` already has a store, as after sign-in.)"""
    small = create_engine(get_settings().database_url, pool_size=1, max_overflow=0, pool_timeout=1)
    platform_db.SessionLocal.configure(bind=small)
    try:
        yield
    finally:
        platform_db.SessionLocal.configure(bind=platform_db.engine)
        small.dispose()


@pytest.fixture
def signed_in(user):
    """A client signed in through the real session cookie, so `CurrentUser` loads the user with
    the request's own session."""
    return signed_in_client(user)


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
