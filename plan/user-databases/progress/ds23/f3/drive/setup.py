"""A user (github id -2300) whose store failed provisioning and stayed failed, with a document
uploaded before data stores whose id-only ingest message runs out its retries. Then the store is
made ready, as the next deploy's `migrate_all` would."""

from sqlalchemy import delete, select, update

from aios import worker
from aios.datastores.resolve import data_store_for
from aios.db import SessionLocal
from aios.models import DataStore, DataStoreStatus, Document, User

with SessionLocal() as db:
    db.execute(delete(User).where(User.github_id == -2300))
    db.commit()
    user = User(github_id=-2300, login="ds23-check")
    db.add(user)
    db.commit()
    doc = Document(owner_id=user.id, filename="legacy.md", data=b"# Old\n\nQueued before stores.")
    db.add(doc)
    db.commit()
    store_id = data_store_for(user.id)
    db.execute(update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.failed))
    db.commit()
    doc_id = doc.id

result = worker.ingest_document_task.apply(args=(doc_id,))  # eager: all 60 retries at once
print("ingest task:", result.state, type(result.result).__name__)

with SessionLocal() as db:
    db.execute(update(DataStore).where(DataStore.id == store_id).values(status=DataStoreStatus.ready))
    db.commit()
    d = db.get(Document, doc_id)
    print("document after the store is ready:", d.status.value, "|", d.error)
