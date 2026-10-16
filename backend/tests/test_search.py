import asyncio
import math
import uuid

import pytest
from conftest import delete_user, provisioned_user

from aios import search
from aios.auth import current_user
from aios.datastores import data_store_for, store_session
from aios.datastores.models import Chunk, Document, DocumentStatus
from aios.main import app
from aios.models import User
from aios.search import hybrid_search, hybrid_search_with_vector

DIM = 1024


def unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def blend(a: int, b: int) -> list[float]:
    v = [0.0] * DIM
    v[a] = v[b] = 1 / math.sqrt(2)
    return v


def add_chunks(
    store_id: uuid.UUID, filename: str, chunks: list[tuple[str, list[float]]], **meta
) -> None:
    """A ready document in the store, of these chunks (text, embedding)."""
    doc = Document(filename=filename, data=b"", status=DocumentStatus.ready)
    doc.chunks = [
        Chunk(ordinal=i, text=t, contextualized=t, meta=meta, embedding=vec)
        for i, (t, vec) in enumerate(chunks)
    ]
    with store_session(store_id, "rw") as store:
        store.add(doc)
        store.commit()


def test_hybrid_search_fuses_semantic_and_lexical(user):
    store_id = data_store_for(user.id)
    texts = {
        "semantic": ("Cats are small domesticated felines.", unit(0)),
        "lexical": ("The zebrafish genome was sequenced in 2013.", unit(1)),
        "both": ("Zebrafish are popular in genetics labs.", blend(0, 1)),
    }
    add_chunks(store_id, "notes.md", list(texts.values()))

    with store_session(store_id, "ro") as store:
        hits = hybrid_search_with_vector(store, query="zebrafish", query_vector=unit(0), limit=5)
    by_text = {h.text: h for h in hits}

    semantic, lexical, both = (by_text[t] for t, _ in texts.values())

    # The keyword matches get a boost from the full-text branch...
    assert both.lexical_rank is not None and lexical.lexical_rank is not None
    assert semantic.lexical_rank is None
    # ...so chunks that rank in both lists beat the best pure vector match.
    assert both.score > semantic.score
    assert lexical.score > semantic.score


def test_search_never_returns_another_users_documents(db, user, monkeypatch):
    """Each store is a database of its own, which only its own roles may connect to
    (test_provisioning): a search runs in one store, so it can't see another's rows."""
    other = provisioned_user(db, -2, "other-user")
    try:
        add_chunks(data_store_for(user.id), "mine.md", [("Zebrafish fins.", unit(0))])
        add_chunks(data_store_for(other.id), "theirs.md", [("Zebrafish genome.", unit(0))])

        async def embed_query(text):
            return unit(0)

        monkeypatch.setattr(search, "embed_query", embed_query)
        for owner, filename in ((user, "mine.md"), (other, "theirs.md")):
            hits = asyncio.run(hybrid_search(data_store_id=data_store_for(owner.id), query="fish"))
            assert [h.filename for h in hits] == [filename]
    finally:
        delete_user(db, other)


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},
        {"q": "   "},
        {"q": "x", "limit": 0},
        {"q": "x", "limit": -1},
        {"q": "x", "limit": 51},
    ],
)
def test_search_endpoint_rejects_bad_params(client, params):
    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")
    try:
        resp = client.get("/api/search", params=params)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_hybrid_search_blank_query_skips_embedding():
    # Would hit the embedding API (and fail without a key) if not short-circuited.
    assert asyncio.run(hybrid_search(data_store_id=uuid.uuid4(), query="  ")) == []
