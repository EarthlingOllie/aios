from sqlalchemy import select

from aios.api import documents
from aios.auth import current_user
from aios.main import app
from aios.models import Document


def test_upload_enqueue_failure_returns_503_and_leaves_no_row(client, db, user, monkeypatch):
    def broker_down(*args, **kwargs):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(documents.ingest_document_task, "delay", broker_down)
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = client.post("/api/documents", files={"file": ("a.txt", b"hello", "text/plain")})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    db.expire_all()
    assert db.scalars(select(Document).where(Document.owner_id == user.id)).all() == []
