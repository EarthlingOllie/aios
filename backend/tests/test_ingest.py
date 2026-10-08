"""End-to-end ingestion with a whitespace tokenizer and fake embeddings (no network)."""

import pytest

pytest.importorskip("docling")

from docling.chunking import HybridChunker  # noqa: E402
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer  # noqa: E402

from aios import ingest  # noqa: E402
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
