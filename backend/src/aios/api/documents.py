from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.auth import CurrentUser
from aios.config import get_settings
from aios.db import get_db
from aios.models import Chunk, Document, DocumentStatus
from aios.search import hybrid_search
from aios.worker import ingest_document_task

router = APIRouter(prefix="/api")
DB = Annotated[Session, Depends(get_db)]


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


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(user: CurrentUser, db: DB):
    rows = db.execute(
        select(Document, func.count(Chunk.id))
        .outerjoin(Chunk)
        .where(Document.owner_id == user.id)
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
async def upload_document(file: UploadFile, user: CurrentUser, db: DB):
    max_bytes = get_settings().max_upload_bytes
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
    doc = Document(
        owner_id=user.id,
        filename=file.filename or "upload",
        content_type=file.content_type,
        data=data,
    )
    db.add(doc)
    db.commit()
    ingest_document_task.delay(doc.id)
    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        status=doc.status,
        error=None,
        chunk_count=0,
        created_at=doc.created_at,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: int, user: CurrentUser, db: DB) -> None:
    doc = db.get(Document, document_id)
    if doc is None or doc.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    db.delete(doc)
    db.commit()


@router.get("/search", response_model=list[SearchHitOut])
async def search(q: str, user: CurrentUser, limit: int = 8):
    return await hybrid_search(owner_id=user.id, query=q, limit=min(limit, 50))
