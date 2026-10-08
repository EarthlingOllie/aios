import math

from aios.models import Chunk, Document, DocumentStatus
from aios.search import hybrid_search_with_vector

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
