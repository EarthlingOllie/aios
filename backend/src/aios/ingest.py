"""Document ingestion: Docling conversion -> HybridChunker -> embeddings -> chunks table.

Docling is only installed in the worker image (the `ingest` extra), so it's imported lazily.
"""

import asyncio
from functools import lru_cache
from io import BytesIO

from sqlalchemy import delete
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.embeddings import embed_documents
from aios.models import Chunk, Document, DocumentStatus


@lru_cache
def _converter():
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


@lru_cache
def _chunker():
    from docling.chunking import HybridChunker

    settings = get_settings()
    return HybridChunker(tokenizer=settings.chunk_tokenizer, max_tokens=settings.chunk_max_tokens)


def chunk_document(filename: str, data: bytes) -> list[dict]:
    from docling.datamodel.base_models import DocumentStream

    result = _converter().convert(DocumentStream(name=filename, stream=BytesIO(data)))
    chunker = _chunker()
    chunks = []
    for chunk in chunker.chunk(dl_doc=result.document):
        pages = sorted(
            {prov.page_no for item in chunk.meta.doc_items for prov in item.prov if prov.page_no}
        )
        chunks.append(
            {
                "text": chunk.text,
                "contextualized": chunker.contextualize(chunk=chunk),
                "meta": {"headings": chunk.meta.headings or [], "pages": pages},
            }
        )
    return chunks


def ingest_document(db: Session, document_id: int) -> None:
    doc = db.get(Document, document_id)
    if doc is None:
        return
    doc.status = DocumentStatus.processing
    doc.error = None
    db.commit()

    try:
        pieces = chunk_document(doc.filename, doc.data)
        vectors = asyncio.run(embed_documents([p["contextualized"] for p in pieces]))
        db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        db.add_all(
            Chunk(document_id=doc.id, ordinal=i, embedding=vec, **piece)
            for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=True))
        )
        doc.status = DocumentStatus.ready
        db.commit()
    except Exception as exc:
        db.rollback()
        doc.status = DocumentStatus.failed
        doc.error = f"{type(exc).__name__}: {exc}"[:2000]
        db.commit()
        raise
