p = "tests/test_search.py"
s = open(p).read()


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    """from aios.auth import current_user
from aios.main import app
from aios.models import Chunk, Document, DocumentStatus, User
from aios.search import hybrid_search, hybrid_search_with_vector
""",
    """from aios.auth import current_user
from aios.datastores import data_store_for, store_session
from aios.datastores.models import Chunk, Document, DocumentStatus
from aios.main import app
from aios.models import User
from aios.search import hybrid_search, hybrid_search_with_vector
""",
)
rep(
    '''def test_hybrid_search_fuses_semantic_and_lexical(db, user):
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
''',
    '''def add_chunks(
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
        hits = hybrid_search_with_vector(
            store, query="zebrafish", query_vector=unit(0), limit=5
        )
''',
)
rep(
    '''def test_hybrid_search_is_scoped_to_owner(db, user):
    hits = hybrid_search_with_vector(
        db, owner_id=user.id + 10_000, query="zebrafish", query_vector=unit(0)
    )
    assert hits == []
''',
    '''def test_search_never_returns_another_users_documents(db, user, monkeypatch):
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
''',
)
rep("import pytest\n", "import pytest\nfrom conftest import delete_user, provisioned_user\n")
rep("from aios.auth import current_user\n", "from aios import search\nfrom aios.auth import current_user\n")
open(p, "w").write(s)
