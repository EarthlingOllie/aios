p = "tests/test_data_stores.py"
s = open(p).read()
assert "busy_store" not in s
s += '''

@pytest.fixture
def busy_store(db, user) -> Iterator[uuid.UUID]:
    """`user`'s store, with its read-write role holding every connection it may."""
    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == user.id))
    names = provision.StoreNames.of(store)
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as provisioner:
        provisioner.execute(
            sql.SQL("ALTER ROLE {} CONNECTION LIMIT 1").format(sql.Identifier(names.rw))
        )
    with store_engine(store.id, "rw").connect():
        yield store.id


def test_a_busy_store_answers_503_to_try_again(api, busy_store, monkeypatch):
    queued = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: queued.append(a))

    r = api.post("/api/documents", files={"file": ("a.md", b"# Hi", "text/markdown")})

    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "Your data store is busy. Try again in a moment."}
    assert r.headers["Retry-After"] == "1"
    assert queued == []
    with pytest.raises(DataStoreBusy), store_session(busy_store, "rw") as store:
        store.execute(text("SELECT 1"))


def test_the_ingest_task_tries_a_busy_store_again(busy_store, monkeypatch):
    retried = []

    def retry(**kwargs):
        retried.append(kwargs)
        return Retry()

    monkeypatch.setattr(worker.ingest_document_task, "retry", retry)

    with pytest.raises(Retry):
        worker.ingest_document_task.run(str(busy_store), 1)

    assert retried == [{"countdown": worker.BUSY_RETRY_COUNTDOWN}]
'''


def rep(a, b):
    global s
    assert a in s, a
    s = s.replace(a, b)


rep(
    """from aios.datastores import (
    DataStoreNotFound,
    DataStoreNotReady,
    data_store_for,
    store_session,
)""",
    """from aios.config import get_settings, libpq_conninfo
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    data_store_for,
    provision,
    store_engine,
    store_session,
)""",
)
rep("import pytest\nfrom conftest", "import psycopg\nimport pytest\nfrom celery.exceptions import Retry\nfrom conftest")
rep("from sqlalchemy import event, select, text, update", "from psycopg import sql\nfrom sqlalchemy import event, select, text, update")
open(p, "w").write(s)
