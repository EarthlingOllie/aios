p = "tests/test_tools.py"
s = open(p).read()


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    '''@pytest.fixture
def documents(db, user):
    """Three chunks of `user`'s, plus one of another user's that matches the same query."""
    other = User(github_id=-2, login="other-user")
    db.add(other)
    db.flush()
    docs = [
        Document(owner_id=user.id, filename="fish.md", data=b"", status=DocumentStatus.ready),
        Document(owner_id=other.id, filename="theirs.md", data=b"", status=DocumentStatus.ready),
    ]
    texts = [
        ["Zebrafish are popular in genetics labs.", "The zebrafish genome was sequenced."],
        ["Their zebrafish notes."],
    ]
    for doc, chunks in zip(docs, texts, strict=True):
        doc.chunks = [
            Chunk(
                ordinal=i, text=t, contextualized=t, meta={"headings": ["Fish"]}, embedding=unit(0)
            )
            for i, t in enumerate(chunks)
        ]
    docs[0].chunks.append(
        Chunk(ordinal=2, text="Cats purr.", contextualized="Cats purr.", meta={}, embedding=unit(1))
    )
    db.add_all(docs)
    db.commit()
    yield
    db.delete(docs[0])
    db.delete(other)  # and, by the database's cascade, their document
    db.commit()
''',
    '''@pytest.fixture
def documents(db, user):
    """Three chunks in `user`'s store, plus one in another user's that matches the same query."""
    other = provisioned_user(db, -2, "other-user")
    fish = ["Zebrafish are popular in genetics labs.", "The zebrafish genome was sequenced."]
    add_chunks(data_store_for(user.id), "fish.md", [(t, unit(0)) for t in fish], headings=["Fish"])
    add_chunks(data_store_for(user.id), "cats.md", [("Cats purr.", unit(1))])
    add_chunks(
        data_store_for(other.id), "theirs.md", [("Their zebrafish notes.", unit(0))], headings=["Fish"]
    )
    yield
    delete_user(db, other)
''',
)
rep(
    """    assert spy["deps"] == ChatDeps(user_id=user.id, run_id=run["id"])""",
    """    assert spy["deps"] == ChatDeps(
        user_id=user.id, run_id=run["id"], data_store_id=data_store_for(user.id)
    )""",
)
rep(
    '''def test_hybrid_search_fills_a_limit_above_its_candidates(db, user, documents, monkeypatch):
    # A query with no keyword matches, so every result comes from the semantic branch.
    monkeypatch.setattr(get_settings(), "search_candidates", 1)
    hits = search.hybrid_search_with_vector(
        db, owner_id=user.id, query="xylophone", query_vector=unit(0), limit=3
    )
''',
    '''def test_hybrid_search_fills_a_limit_above_its_candidates(user, documents, monkeypatch):
    # A query with no keyword matches, so every result comes from the semantic branch.
    monkeypatch.setattr(get_settings(), "search_candidates", 1)
    with store_session(data_store_for(user.id), "ro") as store:
        hits = search.hybrid_search_with_vector(
            store, query="xylophone", query_vector=unit(0), limit=3
        )
''',
)
rep(
    "from aios.models import Agent, Chunk, Document, DocumentStatus, User\n",
    "from aios.datastores import data_store_for, store_session\nfrom aios.models import Agent, User\n",
)
rep("from test_search import unit\n", "from test_search import add_chunks, unit\n")
rep("import pytest\n", "import pytest\nfrom conftest import delete_user, provisioned_user\n")
open(p, "w").write(s)
