"""End-to-end ingestion with a whitespace tokenizer and fake embeddings (no network)."""

import pytest

pytest.importorskip("docling")

from docling.chunking import HybridChunker  # noqa: E402
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from aios import ingest  # noqa: E402
from aios.db import SessionLocal  # noqa: E402
from aios.models import Document, DocumentStatus  # noqa: E402

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


def test_ingest_document_stores_chunks(db, user):
    doc = Document(owner_id=user.id, filename="guide.md", data=MARKDOWN)
    db.add(doc)
    db.commit()

    ingest.ingest_document(db, doc.id)

    db.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert len(doc.chunks) >= 2
    assert [c.ordinal for c in doc.chunks] == list(range(len(doc.chunks)))


def _no_conversion(*args, **kwargs):
    raise AssertionError("chunk_document should not run")


def test_ingest_gives_up_after_max_attempts(db, user, monkeypatch):
    # Previous deliveries died mid-conversion (e.g. OOM) and left the row `processing`.
    doc = Document(
        owner_id=user.id,
        filename="huge.pdf",
        data=b"%PDF",
        status=DocumentStatus.processing,
        attempts=ingest.MAX_ATTEMPTS,
    )
    db.add(doc)
    db.commit()
    monkeypatch.setattr(ingest, "chunk_document", _no_conversion)

    ingest.ingest_document(db, doc.id)

    db.refresh(doc)
    assert doc.status == DocumentStatus.failed
    assert doc.attempts == ingest.MAX_ATTEMPTS + 1
    assert "attempts" in doc.error


def test_ingest_skips_finished_document(db, user, monkeypatch):
    doc = Document(owner_id=user.id, filename="guide.md", data=MARKDOWN)
    db.add(doc)
    db.commit()
    ingest.ingest_document(db, doc.id)
    monkeypatch.setattr(ingest, "chunk_document", _no_conversion)

    ingest.ingest_document(db, doc.id)  # duplicate delivery

    db.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert doc.attempts == 1


def test_ingest_tolerates_document_deleted_mid_ingest(db, user, monkeypatch):
    doc = Document(owner_id=user.id, filename="guide.md", data=MARKDOWN)
    db.add(doc)
    db.commit()
    doc_id = doc.id
    real_chunk_document = ingest.chunk_document

    def delete_then_chunk(filename, data):
        with SessionLocal() as other:
            other.execute(delete(Document).where(Document.id == doc_id))
            other.commit()
        return real_chunk_document(filename, data)

    monkeypatch.setattr(ingest, "chunk_document", delete_then_chunk)

    ingest.ingest_document(db, doc_id)  # chunk insert hits the FK; must not raise

    db.expunge_all()
    assert db.get(Document, doc_id) is None
