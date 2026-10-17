import hashlib
import io
import os
import time

import httpx
import pytest
from conftest import delete_user, provisioned_user
from pydantic import SecretStr
from sqlalchemy import event, select

from aios import ingest, worker
from aios.api import documents
from aios.auth import current_user
from aios.config import get_settings
from aios.datastores import data_store_for, files, store_engine, store_session
from aios.datastores.models import Document, DocumentStatus
from aios.main import app

PDF = b"%PDF-1.7 not really, but bytes all the same \x00\xff\xfe"


def stored_keys(store_id) -> list[str]:
    """The keys of the store's objects in the files bucket."""
    listing = files.client().list_objects_v2(
        Bucket=get_settings().data_store_files_bucket, Prefix=f"stores/{store_id}/"
    )
    return [o["Key"] for o in listing.get("Contents", [])]


def upload(api, name="report.pdf", data=PDF, content_type="application/pdf"):
    return api.post("/api/documents", files={"file": (name, data, content_type)})


@pytest.fixture
def queued(monkeypatch) -> list[tuple]:
    """Ingests the uploads queued, instead of sending them to the broker."""
    calls = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: calls.append(a))
    return calls


def test_an_upload_goes_to_s3_under_the_stores_prefix(api, user, queued):
    r = upload(api)

    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["size_bytes"] == len(PDF)
    store_id = data_store_for(user.id)
    key = f"stores/{store_id}/documents/{doc['id']}"
    assert stored_keys(store_id) == [key]
    assert files.read(key) == PDF
    with store_session(store_id, "ro") as store:
        row = store.get(Document, doc["id"])
        assert (row.s3_key, row.size_bytes, row.sha256) == (
            key,
            len(PDF),
            hashlib.sha256(PDF).hexdigest(),
        )
    assert queued == [(str(store_id), doc["id"])]


def test_the_list_gives_each_documents_size(api, queued):
    upload(api, "a.md", b"# Hi", "text/markdown")

    [listed] = api.get("/api/documents").json()

    assert listed["size_bytes"] == 4


def test_upload_enqueue_failure_returns_503_and_leaves_no_row_or_object(
    client, db, user, files_bucket, monkeypatch
):
    def broker_down(*args, **kwargs):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(documents.ingest_document_task, "delay", broker_down)
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = client.post("/api/documents", files={"file": ("a.txt", b"hello", "text/plain")})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    store_id = data_store_for(user.id)
    with store_session(store_id, "ro") as store:
        assert store.scalars(select(Document)).all() == []
    assert stored_keys(store_id) == []


def test_an_upload_whose_insert_fails_leaves_no_object(api, user, queued):
    store_id = data_store_for(user.id)

    def refuse_insert(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO aios.documents"):
            raise RuntimeError("insert refused")

    rw = store_engine(store_id, "rw")
    event.listen(rw, "before_cursor_execute", refuse_insert)
    try:
        with pytest.raises(RuntimeError, match="insert refused"):
            upload(api)
    finally:
        event.remove(rw, "before_cursor_execute", refuse_insert)

    assert stored_keys(store_id) == []
    assert queued == []


def test_an_upload_that_s3_refuses_answers_503_and_leaves_no_row(api, user, queued, monkeypatch):
    def s3_down(*args, **kwargs):
        raise ConnectionError("S3 unreachable")

    monkeypatch.setattr(files, "put", s3_down)

    r = upload(api)

    assert r.status_code == 503, r.text
    with store_session(data_store_for(user.id), "ro") as store:
        assert store.scalars(select(Document)).all() == []
    assert queued == []


def test_an_upload_whose_reply_from_s3_is_lost_leaves_no_object(api, user, queued, monkeypatch):
    put = files.put

    def stored_but_reply_lost(key, file, content_type):
        put(key, file, content_type)
        raise ConnectionError("connection reset reading the reply")

    monkeypatch.setattr(files, "put", stored_but_reply_lost)

    assert upload(api).status_code == 503

    assert stored_keys(data_store_for(user.id)) == []
    assert queued == []


def test_an_upload_over_the_limit_is_refused_before_s3(api, user, queued, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 3)

    assert upload(api, data=b"four").status_code == 413

    assert stored_keys(data_store_for(user.id)) == []
    assert queued == []


def test_ingest_reads_the_upload_from_s3(api, user, queued, monkeypatch):
    seen = []

    def chunk_document(filename, data):
        seen.append((filename, data))
        return [{"text": "Hi", "contextualized": "Hi", "meta": {}}]

    async def embed_documents(texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    monkeypatch.setattr(ingest, "chunk_document", chunk_document)
    monkeypatch.setattr(ingest, "embed_documents", embed_documents)
    doc = upload(api).json()

    for args in queued:
        worker.ingest_document_task.run(*args)

    assert seen == [("report.pdf", PDF)]
    [listed] = api.get("/api/documents").json()
    assert (listed["id"], listed["status"], listed["chunk_count"]) == (doc["id"], "ready", 1)


def test_ingest_fails_a_document_whose_object_is_gone(api, user, queued, monkeypatch):
    monkeypatch.setattr(ingest, "chunk_document", lambda *a: pytest.fail("nothing to chunk"))
    doc = upload(api).json()
    store_id = data_store_for(user.id)
    files.delete(files.document_key(store_id, doc["id"]))

    with pytest.raises(Exception, match="NoSuchKey"):
        worker.ingest_document_task.run(str(store_id), doc["id"])

    with store_session(store_id, "ro") as store:
        row = store.get(Document, doc["id"])
        assert row.status == DocumentStatus.failed
        assert "NoSuchKey" in row.error


def test_deleting_a_document_deletes_its_object(api, user, queued):
    doc = upload(api).json()
    store_id = data_store_for(user.id)
    assert stored_keys(store_id) != []

    assert api.delete(f"/api/documents/{doc['id']}").status_code == 204

    assert stored_keys(store_id) == []
    assert api.get("/api/documents").json() == []


def test_a_delete_that_s3_refuses_keeps_the_document(api, user, queued, monkeypatch):
    doc = upload(api).json()

    def s3_down(key):
        raise ConnectionError("S3 unreachable")

    monkeypatch.setattr(files, "delete", s3_down)

    assert api.delete(f"/api/documents/{doc['id']}").status_code == 503

    assert [d["id"] for d in api.get("/api/documents").json()] == [doc["id"]]


def test_download_redirects_to_a_short_lived_link_to_the_original_file(api, queued):
    name = "Résumé (final), 2026.pdf"
    doc = upload(api, name).json()

    r = api.get(f"/api/documents/{doc['id']}/download", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["Cache-Control"] == "no-store"
    link = r.headers["Location"]
    assert f"X-Amz-Expires={get_settings().document_download_url_seconds}" in link
    got = httpx.get(link)
    assert got.status_code == 200
    assert got.content == PDF
    disposition = got.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20%28final%29%2C%202026.pdf" in disposition
    # Browsers without filename* support get an ASCII stand-in.
    assert 'filename="R_sum_ (final), 2026.pdf"' in disposition


def test_the_download_link_is_refused_once_it_expires(api, queued, monkeypatch):
    # A link's signing time is to the second, so it can expire up to a second early: 3 seconds
    # leaves room to use it once first.
    monkeypatch.setattr(get_settings(), "document_download_url_seconds", 3)
    doc = upload(api).json()
    link = api.get(f"/api/documents/{doc['id']}/download", follow_redirects=False).headers[
        "Location"
    ]
    assert httpx.get(link).status_code == 200

    time.sleep(3.5)

    expired = httpx.get(link)
    assert expired.status_code == 403
    assert "expired" in expired.text


def test_each_download_mints_its_own_link(api, queued):
    doc = upload(api).json()
    path = f"/api/documents/{doc['id']}/download"

    first = api.get(path, follow_redirects=False).headers["Location"]
    time.sleep(1.1)  # signatures carry the time to the second
    second = api.get(path, follow_redirects=False).headers["Location"]

    assert first != second


def test_another_stores_document_is_a_404(api, db, user, queued):
    other = provisioned_user(db, -7, "other-user")
    try:
        with store_session(data_store_for(other.id), "rw") as store:
            doc = Document(
                filename="theirs.pdf",
                s3_key=files.document_key(data_store_for(other.id), 1),
                size_bytes=1,
                sha256="0" * 64,
            )
            store.add(doc)
            store.commit()
            theirs = doc.id

        r = api.get(f"/api/documents/{theirs}/download", follow_redirects=False)

        assert r.status_code == 404
        assert "Location" not in r.headers
    finally:
        delete_user(db, other)


def test_a_missing_document_is_a_404(api):
    assert api.get("/api/documents/999999/download", follow_redirects=False).status_code == 404


def test_the_ascii_filename_stands_in_for_any_name():
    assert files.attachment('a"b\\c\r\n.pdf') == (
        "attachment; filename=\"a_b_c__.pdf\"; filename*=UTF-8''a%22b%5Cc%0D%0A.pdf"
    )
    assert files.attachment("") == "attachment; filename=\"download\"; filename*=UTF-8''download"


@pytest.fixture
def fresh_files_clients():
    """The files clients made anew for the test, and again after it."""
    files.client.cache_clear()
    files._signer.cache_clear()
    yield
    files.client.cache_clear()
    files._signer.cache_clear()


def test_the_files_servers_keys_can_come_from_the_settings(
    files_bucket, fresh_files_clients, monkeypatch
):
    # As a local run with only backend/.env: boto3 reads the environment, not that file.
    settings = get_settings()
    access_key = settings.data_store_files_access_key or os.environ["AWS_ACCESS_KEY_ID"]
    secret_key = (
        settings.data_store_files_secret_key.get_secret_value()
        or os.environ["AWS_SECRET_ACCESS_KEY"]
    )
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "data_store_files_access_key", access_key)
    monkeypatch.setattr(settings, "data_store_files_secret_key", SecretStr(secret_key))
    key = "stores/settings-check/documents/1"

    files.put(key, io.BytesIO(b"hi"), None)

    assert files.read(key) == b"hi"
    assert httpx.get(files.download_url(key, "a")).content == b"hi"
    files.delete(key)
