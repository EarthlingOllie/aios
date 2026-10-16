p = "tests/test_data_stores.py"
s = open(p).read()


def rep(old, new):
    global s
    assert old in s, old[:80]
    s = s.replace(old, new)


rep(
    '''from aios.datastores import (
    DataStoreNotFound,
    DataStoreNotReady,
    data_store_for,
    documents_session,
    store_session,
)
from aios.db import SessionLocal, engine
from aios.models import (
    DataStore,
    DataStoreStatus,
    Document,
    DocumentStatus,
    User,
    WorkflowRun,
    WorkflowRunStatus,
)
''',
    '''from aios.datastores import (
    DataStoreNotFound,
    DataStoreNotReady,
    data_store_for,
    store_session,
)
from aios.datastores.models import Document, DocumentStatus
from aios.db import SessionLocal, engine
from aios.models import (
    DataStore,
    DataStoreStatus,
    User,
    WorkflowRun,
    WorkflowRunStatus,
)
''',
)
rep(
    '''    store_id = data_store_for(user.id)
    with documents_session(store_id, "ro") as store:
        assert store.scalar(text("SELECT 1")) == 1

    for opener in (store_session, documents_session):
        with pytest.raises(DataStoreNotFound), opener(uuid.uuid4(), "rw"):
            pass
        with pytest.raises(ValueError, match="role"), opener(store_id, "admin"):
            pass
    db.execute(
        update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.failed)
    )
    db.commit()
    for opener in (store_session, documents_session):
        with pytest.raises(DataStoreNotReady), opener(store_id, "ro"):
            pass
''',
    '''    store_id = data_store_for(user.id)
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
''',
)
start = s.index("def test_the_ingest_task_ingests_in_the_store_it_is_given")
end = s.index("@pytest.fixture\ndef searches")
s = (
    s[:start]
    + '''def add_document(store_id: uuid.UUID, **values) -> int:
    """A document in the store; returns its id."""
    with store_session(store_id, "rw") as store:
        doc = Document(**{"filename": "a.md", "data": b"# Hi", **values})
        store.add(doc)
        store.commit()
        return doc.id


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


'''
    + s[end:]
)
rep("from fake_model import ToolCall", "from conftest import delete_user, provisioned_user\nfrom fake_model import ToolCall")
rep("from aios import auth, ingest, worker, workflows", "from aios import auth, ingest, search, worker, workflows")
open(p, "w").write(s)
