p = "tests/test_ingest.py"
s = open(p).read()
start = s.index("def test_ingest_document_stores_chunks")
head = s[:start]
head = head.replace(
    """from sqlalchemy import delete  # noqa: E402

from aios import ingest  # noqa: E402
from aios.db import SessionLocal  # noqa: E402
from aios.models import Document, DocumentStatus  # noqa: E402
""",
    """from sqlalchemy import delete  # noqa: E402

from aios import ingest  # noqa: E402
from aios.datastores import data_store_for, store_session  # noqa: E402
from aios.datastores.models import Document, DocumentStatus  # noqa: E402
""",
)
head = head.replace(
    '"""End-to-end ingestion with a whitespace tokenizer and fake embeddings (no network)."""',
    '"""End-to-end ingestion, in the user\'s data store, with a whitespace tokenizer and fake\nembeddings (no network)."""',
)
body = '''@pytest.fixture
def store(user):
    """A read-write session on `user`'s store."""
    with store_session(data_store_for(user.id), "rw") as session:
        yield session


def add(store, **values) -> Document:
    doc = Document(**{"filename": "guide.md", "data": MARKDOWN, **values})
    store.add(doc)
    store.commit()
    return doc


def test_ingest_document_stores_chunks(store):
    doc = add(store)

    ingest.ingest_document(store, doc.id)

    store.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert len(doc.chunks) >= 2
    assert [c.ordinal for c in doc.chunks] == list(range(len(doc.chunks)))


def _no_conversion(*args, **kwargs):
    raise AssertionError("chunk_document should not run")


def test_ingest_gives_up_after_max_attempts(store, monkeypatch):
    # Previous deliveries died mid-conversion (e.g. OOM) and left the row `processing`.
    doc = add(
        store,
        filename="huge.pdf",
        data=b"%PDF",
        status=DocumentStatus.processing,
        attempts=ingest.MAX_ATTEMPTS,
    )
    monkeypatch.setattr(ingest, "chunk_document", _no_conversion)

    ingest.ingest_document(store, doc.id)

    store.refresh(doc)
    assert doc.status == DocumentStatus.failed
    assert doc.attempts == ingest.MAX_ATTEMPTS + 1
    assert "attempts" in doc.error


def test_ingest_skips_finished_document(store, monkeypatch):
    doc = add(store)
    ingest.ingest_document(store, doc.id)
    monkeypatch.setattr(ingest, "chunk_document", _no_conversion)

    ingest.ingest_document(store, doc.id)  # duplicate delivery

    store.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert doc.attempts == 1


def test_ingest_tolerates_document_deleted_mid_ingest(user, store, monkeypatch):
    doc_id = add(store).id
    real_chunk_document = ingest.chunk_document

    def delete_then_chunk(filename, data):
        with store_session(data_store_for(user.id), "rw") as other:
            other.execute(delete(Document).where(Document.id == doc_id))
            other.commit()
        return real_chunk_document(filename, data)

    monkeypatch.setattr(ingest, "chunk_document", delete_then_chunk)

    ingest.ingest_document(store, doc_id)  # chunk insert hits the FK; must not raise

    store.expunge_all()
    assert store.get(Document, doc_id) is None
'''
open(p, "w").write(head + body)
