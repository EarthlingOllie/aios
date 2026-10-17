import asyncio
import math
import uuid

import pytest

from aios.auth import current_data_store, current_user
from aios.main import app
from aios.models import Chunk, Document, DocumentStatus, User
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


def test_hybrid_search_fuses_semantic_and_lexical(db, user):
    doc = Document(owner_id=user.id, filename="notes.md", data=b"", status=DocumentStatus.ready)
    texts = {
        "semantic": ("Cats are small domesticated felines.", unit(0)),
        "lexical": ("The zebrafish genome was sequenced in 2013.", unit(1)),
        "both": ("Zebrafish are popular in genetics labs.", blend(0, 1)),
    }
    doc.chunks = [
        Chunk(ordinal=i, text=t, contextualized=t, meta={}, embedding=vec)
        for i, (t, vec) in enumerate(texts.values())
    ]
    db.add(doc)
    db.commit()

    hits = hybrid_search_with_vector(
        db, owner_id=user.id, query="zebrafish", query_vector=unit(0), limit=5
    )
    by_text = {h.text: h for h in hits}

    semantic, lexical, both = (by_text[t] for t, _ in texts.values())

    # The keyword matches get a boost from the full-text branch...
    assert both.lexical_rank is not None and lexical.lexical_rank is not None
    assert semantic.lexical_rank is None
    # ...so chunks that rank in both lists beat the best pure vector match.
    assert both.score > semantic.score
    assert lexical.score > semantic.score


def test_hybrid_search_is_scoped_to_owner(db, user):
    hits = hybrid_search_with_vector(
        db, owner_id=user.id + 10_000, query="zebrafish", query_vector=unit(0)
    )
    assert hits == []


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
    app.dependency_overrides[current_data_store] = uuid.uuid4
    try:
        resp = client.get("/api/search", params=params)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_hybrid_search_blank_query_skips_embedding():
    # Would hit the embedding API (and fail without a key) if not short-circuited.
    assert asyncio.run(hybrid_search(data_store_id=uuid.uuid4(), query="  ")) == []
