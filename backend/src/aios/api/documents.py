import logging
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from aios.auth import CurrentUser
from aios.config import get_settings
from aios.datastores import data_store_for, files, store_session, store_status
from aios.datastores.models import Chunk, Document, DocumentStatus
from aios.db import DbId
from aios.models import DataStoreStatus
from aios.search import hybrid_search
from aios.worker import ingest_document_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Where documents' ids come from: an upload takes its id before its row is inserted.
DOCUMENT_IDS = f"{Document.__table__.fullname}_id_seq"


class DocumentOut(BaseModel):
    id: int
    filename: str
    status: DocumentStatus
    error: str | None
    chunk_count: int
    size_bytes: int
    created_at: datetime


class SearchHitOut(BaseModel):
    chunk_id: int
    document_id: int
    filename: str
    text: str
    meta: dict
    score: float
    semantic_rank: int | None
    lexical_rank: int | None


class UserOut(BaseModel):
    id: int
    login: str
    name: str | None
    avatar_url: str | None


class DataStoreOut(BaseModel):
    # Only its status: a store's name, roles and credentials never leave the platform.
    status: DataStoreStatus


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.get("/data-store", response_model=DataStoreOut)
def data_store(user: CurrentUser):
    """The user's data store's status: documents and search wait until it's `ready`."""
    return DataStoreOut(status=store_status(data_store_for(user.id)))


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(user: CurrentUser):
    with store_session(data_store_for(user.id), "ro") as db:
        rows = db.execute(
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk)
            .group_by(Document.id)
            .order_by(Document.created_at.desc())
        ).all()
    return [
        DocumentOut(
            id=d.id,
            filename=d.filename,
            status=d.status,
            error=d.error,
            chunk_count=n,
            size_bytes=d.size_bytes,
            created_at=d.created_at,
        )
        for d, n in rows
    ]


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def upload_document(file: UploadFile, user: CurrentUser):
    # Plain `def`: FastAPI runs it in the threadpool, so the blocking upload to S3, DB commits
    # and broker publish below don't stall the event loop (SSE streams, health checks).
    # The file has been spooled (to disk, if large), so its size is known before it's sent on.
    size = file.file.seek(0, os.SEEK_END)
    file.file.seek(0)
    if size > get_settings().max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
    store_id = data_store_for(user.id)
    with store_session(store_id, "rw") as db:
        # The document's id first, since its file's key holds it. Committed, so no transaction
        # is left open through the upload.
        doc_id = db.scalar(select(func.nextval(DOCUMENT_IDS)))
        db.commit()
        key = files.document_key(store_id, doc_id)
        try:
            stored = files.put(key, file.file, file.content_type)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not store the file; please try again shortly.",
            ) from exc
        # From here, the file is deleted again unless its document makes it to the ingest
        # queue: nothing would ever ingest, list or delete it.
        try:
            doc = Document(
                id=doc_id,
                filename=file.filename or "upload",
                content_type=file.content_type,
                s3_key=key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
            db.add(doc)
            db.flush()
            db.refresh(doc, ["created_at"])  # load the server default now, not lazily after commit
            db.commit()
            try:
                ingest_document_task.delay(str(store_id), doc.id)
            except Exception as exc:
                # Nothing would ever pick up a committed-but-unqueued row; drop it so the client
                # can simply retry without leaving an orphan stuck in `pending`.
                db.delete(doc)
                db.commit()
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Could not queue the document for processing; please try again shortly.",
                ) from exc
        except BaseException:
            _delete_unkept_file(key)
            raise
    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        status=doc.status,
        error=None,
        chunk_count=0,
        size_bytes=doc.size_bytes,
        created_at=doc.created_at,
    )


def _delete_unkept_file(key: str) -> None:
    """Deletes the file of an upload that failed. Should that fail too, it's logged, and the
    upload's own error is the one answered."""
    try:
        files.delete(key)
    except Exception:
        logger.exception("Couldn't delete %s, the file of an upload that failed", key)


@router.get("/documents/{document_id}/download")
def download_document(document_id: DbId, user: CurrentUser) -> RedirectResponse:
    """Redirects to a link that downloads the document's file under its original name. The link
    is minted for this download, and lasts `DOCUMENT_DOWNLOAD_URL_SECONDS`."""
    # Only the user's own store is looked in, as its read-only role.
    with store_session(data_store_for(user.id), "ro") as db:
        doc = db.get(Document, document_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        url = files.download_url(doc.s3_key, doc.filename)
    # Not to be kept (by a cache, say): each download gets a link of its own.
    return RedirectResponse(url, status.HTTP_302_FOUND, headers={"Cache-Control": "no-store"})


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: DbId, user: CurrentUser) -> None:
    with store_session(data_store_for(user.id), "rw") as db:
        doc = db.get(Document, document_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        db.delete(doc)
        db.flush()
        # The file goes before the row's delete is committed: should S3 refuse, the document
        # stays, to be deleted again, rather than leave a file nothing points to.
        try:
            files.delete(doc.s3_key)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not delete the file; please try again shortly.",
            ) from exc
        db.commit()


@router.get("/search", response_model=list[SearchHitOut])
async def search(
    user: CurrentUser,
    # At least one non-whitespace character; blank queries would fail at the embedding API.
    q: Annotated[str, Query(min_length=1, pattern=r"\S")],
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
):
    store_id = await run_in_threadpool(data_store_for, user.id)
    return await hybrid_search(data_store_id=store_id, query=q, limit=limit)
