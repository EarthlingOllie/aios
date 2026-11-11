"""Document ingestion: the file from S3 -> Docling conversion -> HybridChunker -> chunks table,
and each chunk into the search index (`aios.search_index`), in the same transaction, in the
document's data store (on a `store_session` the caller gives it). The chunks are embedded later,
by the store's embedding sweep: found by keyword until then.

Docling is only installed in the worker image (the `ingest` extra), so it's imported lazily.
"""

from functools import lru_cache
from io import BytesIO

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores import DataStoreBusy, files
from aios.datastores.models import Chunk, Document, DocumentStatus
from aios.search_index import index_chunks

# Deliveries before giving up; a doc that crashes the worker would otherwise be redelivered forever.
MAX_ATTEMPTS = 3


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


def set_status(db: Session, document_id: int, status: DocumentStatus, error: str | None) -> bool:
    """UPDATE by id (no ORM instance), so a row deleted meanwhile is a no-op, not StaleDataError."""
    result = db.execute(
        update(Document).where(Document.id == document_id).values(status=status, error=error)
    )
    db.commit()
    return result.rowcount > 0


def ingest_document(db: Session, document_id: int, *, last_try: bool = True) -> None:
    """Ingests the document. Should S3 be unavailable, raises `files.FilesUnavailable` for a
    later try, or, on the `last_try`, fails the document with S3's error."""
    # Atomically claim the row and count this delivery. Finished docs (e.g. a duplicate
    # delivery) are skipped. Its file's key comes back with the claim, and the commit ends the
    # transaction so none sits idle through the download and the (possibly minutes-long)
    # conversion.
    claimed = db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status.in_([DocumentStatus.pending, DocumentStatus.processing]),
        )
        .values(status=DocumentStatus.processing, error=None, attempts=Document.attempts + 1)
        .returning(Document.attempts, Document.filename, Document.s3_key)
    ).one_or_none()
    db.commit()
    if claimed is None:
        return
    if claimed.attempts > MAX_ATTEMPTS:
        # Earlier deliveries died without reaching the except below (OOM kill, hard time limit).
        set_status(
            db,
            document_id,
            DocumentStatus.failed,
            f"Gave up after {MAX_ATTEMPTS} attempts: the worker crashed or timed out "
            "while processing this document.",
        )
        return

    try:
        pieces = chunk_document(claimed.filename, files.read(claimed.s3_key))
        db.execute(delete(Chunk).where(Chunk.document_id == document_id))
        db.add_all(
            Chunk(document_id=document_id, ordinal=i, **piece) for i, piece in enumerate(pieces)
        )
        db.flush()
        index_chunks(db, document_id)
        set_status(db, document_id, DocumentStatus.ready, None)
    except DataStoreBusy:
        raise  # Left processing, for the task's retry to claim again (`aios.worker`).
    except files.FilesUnavailable as exc:
        if last_try:
            if _fail(db, document_id, exc.__cause__):
                raise exc.__cause__ from None
            return
        # S3's doing, not the file's, and it never got to the conversion: left processing, and
        # this delivery not counted against MAX_ATTEMPTS, for the task's retry.
        db.rollback()
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(attempts=Document.attempts - 1)
        )
        db.commit()
        raise
    except Exception as exc:
        if _fail(db, document_id, exc):
            raise


def _fail(db: Session, document_id: int, exc: BaseException) -> bool:
    """Marks the document failed with `exc`; False if it's gone (deleted mid-ingest: the chunk
    insert hit the FK), with nothing left to record."""
    db.rollback()
    return set_status(db, document_id, DocumentStatus.failed, f"{type(exc).__name__}: {exc}"[:2000])
