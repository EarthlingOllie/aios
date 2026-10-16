import uuid

import pytest

from aios import ingest
from aios.datastores import DataStoreNotFound, data_store_for, store_session
from aios.models import Document, DocumentStatus, User
from aios.worker import ingest_document_task


def test_data_store_for_gives_each_user_one_store(db, user):
    store = data_store_for(user)

    assert data_store_for(user).id == store.id
    assert store.owner_user_id == user.id


def test_data_store_for_gives_users_different_stores(db, user):
    other = User(github_id=-2, login="other-user")
    db.add(other)
    db.commit()
    try:
        assert data_store_for(other).id != data_store_for(user).id
    finally:
        db.delete(other)
        db.commit()


def test_store_session_refuses_a_store_that_doesnt_exist(db):
    with pytest.raises(DataStoreNotFound), store_session(uuid.uuid4(), "ro"):
        pass


def test_ingest_task_ingests_into_the_given_store(db, user, monkeypatch):
    store = data_store_for(user)
    doc = Document(owner_id=user.id, filename="notes.md", data=b"# Notes")
    db.add(doc)
    db.commit()

    monkeypatch.setattr(
        ingest,
        "chunk_document",
        lambda filename, data: [{"text": "Notes", "contextualized": "Notes", "meta": {}}],
    )

    async def embed(texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(ingest, "embed_documents", embed)

    # As Celery delivers it: JSON, so the store's id is a string.
    ingest_document_task.run(str(store.id), doc.id)

    db.refresh(doc)
    assert doc.status == DocumentStatus.ready
    assert [c.text for c in doc.chunks] == ["Notes"]
