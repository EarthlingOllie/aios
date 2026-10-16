from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func, select

from aios.auth import CurrentUser
from aios.config import get_settings
from aios.datastores import data_store_for, store_session, store_status
from aios.datastores.models import Chunk, Document, DocumentStatus
from aios.db import DbId
from aios.models import DataStoreStatus
from aios.search import hybrid_search
from aios.worker import ingest_document_task

router = APIRouter(prefix="/api")


class DocumentOut(BaseModel):
    id: int
    filename: str
    status: DocumentStatus
    error: str | None
    chunk_count: int
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
            created_at=d.created_at,
        )
        for d, n in rows
    ]


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def upload_document(file: UploadFile, user: CurrentUser):
    # Plain `def`: FastAPI runs it in the threadpool, so the blocking file read, DB commit
    # and broker publish below don't stall the event loop (SSE streams, health checks).
    max_bytes = get_settings().max_upload_bytes
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
    store_id = data_store_for(user.id)
    with store_session(store_id, "rw") as db:
        doc = Document(
            filename=file.filename or "upload",
            content_type=file.content_type,
            data=data,
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
    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        status=doc.status,
        error=None,
        chunk_count=0,
        created_at=doc.created_at,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: DbId, user: CurrentUser) -> None:
    with store_session(data_store_for(user.id), "rw") as db:
        doc = db.get(Document, document_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        db.delete(doc)
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
