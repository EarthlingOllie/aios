"""End-to-end ingestion, in the organisation's data store, with a whitespace tokenizer and fake
embeddings (no network)."""

import pytest

pytest.importorskip("docling")

from conftest import add_document, store_of  # noqa: E402
from docling.chunking import HybridChunker  # noqa: E402
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from aios import ingest  # noqa: E402
from aios.datastores import store_session  # noqa: E402
from aios.datastores.models import Document, DocumentStatus  # noqa: E402

MARKDOWN = b"""# Field guide

An introduction to model organisms.

## Zebrafish

Zebrafish embryos are transparent, which makes them useful for developmental biology.

## Fruit flies

Drosophila has a short generation time.
"""


class WhitespaceTokenizer(BaseTokenizer):
    max_tokens: int = 32

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self):
        return None


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    chunker = HybridChunker(tokenizer=WhitespaceTokenizer())
    monkeypatch.setattr(ingest, "_chunker", lambda: chunker)

    async def fake_embed(texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(ingest, "embed_documents", fake_embed)


def test_chunk_document_keeps_headings():
    chunks = ingest.chunk_document("guide.md", MARKDOWN)
    zebrafish = next(c for c in chunks if "transparent" in c["text"])
    assert "Zebrafish" in zebrafish["meta"]["headings"]
    assert (
        zebrafish["contextualized"].startswith("Field guide")
        or "Zebrafish" in (zebrafish["contextualized"])
    )


@pytest.fixture
def store(user, files_bucket):
    """A read-write session on `user`'s store."""
    with store_session(store_of(user), "rw") as session:
        session.info["store_id"] = store_of(user)
        yield session


def add(store, data: bytes = MARKDOWN, **values) -> Document:
    """A document, with its file in S3, as an upload leaves it."""
    doc_id = add_document(store.info["store_id"], data, **{"filename": "guide.md", **values})
    return store.get(Document, doc_id)


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
        data=b"%PDF",
        filename="huge.pdf",
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
        with store_session(store_of(user), "rw") as other:
            other.execute(delete(Document).where(Document.id == doc_id))
            other.commit()
        return real_chunk_document(filename, data)

    monkeypatch.setattr(ingest, "chunk_document", delete_then_chunk)

    ingest.ingest_document(store, doc_id)  # chunk insert hits the FK; must not raise

    store.expunge_all()
    assert store.get(Document, doc_id) is None
