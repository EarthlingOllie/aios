import logging
import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aios.api.organisation import UserOut, users_out
from aios.auth import CurrentDataStore, CurrentUser
from aios.config import get_settings
from aios.datastores import files, quota, store_session
from aios.datastores.models import Chunk, Document, DocumentStatus, Source, SourceKind
from aios.db import DbId
from aios.models import DataStoreStatus
from aios.permissions import Action, allow
from aios.sources import uploads_source_id
from aios.worker import ingest_document_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Where documents' ids come from (an upload takes its id before its row is inserted): the id
# column's own sequence, as Postgres knows it.
DOCUMENT_IDS = func.pg_get_serial_sequence(Document.__table__.fullname, "id")


class DocumentSourceOut(BaseModel):
    """The source a document belongs to (`aios.sources`): Uploads for an upload."""

    id: int
    kind: SourceKind
    name: str


class DocumentOut(BaseModel):
    id: int
    filename: str
    status: DocumentStatus
    error: str | None
    chunk_count: int
    size_bytes: int
    # The member who uploaded it; null for documents from before it was recorded, and once
    # their account is gone.
    uploaded_by: UserOut | None
    source: DocumentSourceOut
    created_at: datetime


class UsageOut(BaseModel):
    """What the store uses, as last measured (null until it's first measured), and its caps."""

    db_bytes: int | None
    db_quota_bytes: int
    files_bytes: int | None
    files_quota_bytes: int
    measured_at: datetime | None


class DataStoreOut(BaseModel):
    # Only its status and usage: a store's name, roles and credentials never leave the platform.
    status: DataStoreStatus
    usage: UsageOut


@router.get("/data-store", response_model=DataStoreOut, dependencies=[allow(Action.read)])
def data_store(store_id: CurrentDataStore):
    """The organisation's data store's status (documents and search wait until it's `ready`),
    and its usage against its storage caps."""
    status, usage = quota.status_and_usage(store_id)
    return DataStoreOut(status=status, usage=UsageOut(**asdict(usage)))


@router.get("/documents", response_model=list[DocumentOut], dependencies=[allow(Action.read)])
def list_documents(store_id: CurrentDataStore, source_id: DbId | None = None):
    """The organisation's documents, newest first, with who uploaded each and the source each
    belongs to; only `source_id`'s, if given."""
    query = (
        select(Document, func.count(Chunk.id), Source)
        .join(Source, Document.source_id == Source.id)
        .outerjoin(Chunk)
        .group_by(Document.id, Source.id)
        .order_by(Document.created_at.desc())
    )
    if source_id is not None:
        query = query.where(Document.source_id == source_id)
    with store_session(store_id, "ro") as db:
        rows = db.execute(query).all()
    # Once the store's session is done: the request's own was closed when the store was found.
    by = users_out(store_id, {d.uploaded_by for d, _, _ in rows if d.uploaded_by is not None})
    return [
        DocumentOut(
            id=d.id,
            filename=d.filename,
            status=d.status,
            error=d.error,
            chunk_count=n,
            size_bytes=d.size_bytes,
            uploaded_by=by.get(d.uploaded_by) if d.uploaded_by is not None else None,
            source=_source_out(s),
            created_at=d.created_at,
        )
        for d, n, s in rows
    ]


def _source_out(source: Source) -> DocumentSourceOut:
    return DocumentSourceOut(id=source.id, kind=source.kind, name=source.name)


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.documents)],
)
def upload_document(file: UploadFile, user: CurrentUser, store_id: CurrentDataStore):
    # Plain `def`: FastAPI runs it in the threadpool, so the blocking upload to S3, DB commits
    # and broker publish below don't stall the event loop (SSE streams, health checks).
    # The file has been spooled (to disk, if large), so its size is known before it's sent on
    # (Starlette counts it while parsing the form; one it didn't isn't taken on trust).
    if file.size is None or file.size > get_settings().max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File too large")
    with store_session(store_id, "rw") as db:
        refusal = quota.upload_refusal(db, file.filename or "upload", file.size)
        if refusal:
            raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, refusal)
        # The document's id first, since its file's key holds it. Committed, so no transaction
        # is left open through the upload.
        doc_id = db.scalar(select(func.nextval(DOCUMENT_IDS)))
        # Every upload belongs to the store's Uploads.
        uploads = _source_out(db.get_one(Source, uploads_source_id(db)))
        db.commit()
        key = files.document_key(store_id, doc_id)
        try:
            stored = files.put(key, file.file, file.content_type)
        except Exception as exc:
            # It may have been stored all the same (the reply was lost, say).
            _delete_unkept_file(key)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not store the file; please try again shortly.",
            ) from exc
        # From here, the file is deleted again unless its document makes it to the ingest
        # queue: nothing would ever ingest, list or delete it. But not while a row points to it:
        # one the failed enqueue couldn't remove stays listed, to be downloaded or deleted.
        row_committed = False
        try:
            doc = Document(
                id=doc_id,
                filename=file.filename or "upload",
                content_type=file.content_type,
                s3_key=key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                uploaded_by=user.id,
                source_id=uploads.id,
            )
            db.add(doc)
            db.flush()
            db.refresh(doc, ["created_at"])  # load the server default now, not lazily after commit
            db.commit()
            row_committed = True
            try:
                ingest_document_task.delay(str(store_id), doc.id)
            except Exception as exc:
                # Nothing would ever pick up a committed-but-unqueued row; drop it so the client
                # can simply retry without leaving an orphan stuck in `pending`.
                db.delete(doc)
                db.commit()
                row_committed = False
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "Could not queue the document for processing; please try again shortly.",
                ) from exc
        except BaseException:
            if not row_committed:
                _delete_unkept_file(key)
            raise
        _record_files(store_id, db)
    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        status=doc.status,
        error=None,
        chunk_count=0,
        size_bytes=doc.size_bytes,
        uploaded_by=UserOut.model_validate(user, from_attributes=True),
        source=uploads,
        created_at=doc.created_at,
    )


def _record_files(store_id: uuid.UUID, db: Session) -> None:
    """Records the store's documents' bytes after an upload or delete, for the Data page. Should
    that fail, it's logged: the upload or delete went through, and the schedule records them."""
    try:
        quota.record_files(store_id, db)
    except Exception:
        logger.exception("Couldn't record data store %s's documents' bytes", store_id)


def _delete_unkept_file(key: str) -> None:
    """Deletes the file of an upload that failed. Should that fail too, it's logged, and the
    upload's own error is the one answered."""
    try:
        files.delete(key)
    except Exception:
        logger.exception("Couldn't delete %s, the file of an upload that failed", key)


@router.get("/documents/{document_id}/download", dependencies=[allow(Action.read)])
def download_document(document_id: DbId, store_id: CurrentDataStore) -> RedirectResponse:
    """Redirects to a link that downloads the document's file under its original name. The link
    is minted for this download, and lasts `DOCUMENT_DOWNLOAD_URL_SECONDS`."""
    # Only the organisation's own store is looked in, as its read-only role.
    with store_session(store_id, "ro") as db:
        doc = db.get(Document, document_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        url = files.download_url(doc.s3_key, doc.filename)
    # Not to be kept (by a cache, say): each download gets a link of its own.
    return RedirectResponse(url, status.HTTP_302_FOUND, headers={"Cache-Control": "no-store"})


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.documents)],
)
def delete_document(document_id: DbId, store_id: CurrentDataStore) -> None:
    with store_session(store_id, "rw") as db:
        key = db.scalar(select(Document.s3_key).where(Document.id == document_id))
        if key is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        # Ended, so no transaction (or lock on the row) is held while S3 is slow.
        db.commit()
        # The file goes before the row: should S3 refuse, the document stays, to be deleted
        # again, rather than leave a file nothing points to. (Deleting a file already gone, as
        # that would be, is no error.)
        try:
            files.delete(key)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not delete the file; please try again shortly.",
            ) from exc
        db.execute(delete(Document).where(Document.id == document_id))  # its chunks cascade
        db.commit()
        _record_files(store_id, db)
