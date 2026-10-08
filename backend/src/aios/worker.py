from celery import Celery

from aios.config import get_settings
from aios.db import SessionLocal

settings = get_settings()

# Fire-and-forget tasks: no result backend; progress lives on the documents table.
celery_app = Celery("aios", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
)


@celery_app.task(name="aios.ingest_document")
def ingest_document_task(document_id: int) -> None:
    from aios.ingest import ingest_document

    with SessionLocal() as db:
        ingest_document(db, document_id)
