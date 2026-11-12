"""The outbox (`aios.outbox`): each change writes its event in its own transaction, with its
chain of causes, and a change that rolls back writes none. Then the store is stamped pending."""

import pytest
from conftest import add_document, store_of
from sqlalchemy import select, update
from test_agents import agent_body
from test_chat_agent_invocations import say
from test_chats import add, make_chat, post
from test_data_tools import as_owner, provisioned
from test_schema_change_apply import CONTACTS, approve, propose
from test_triggers import make_agent
from test_write_data import TOOLS, write

from aios import ingest
from aios.datastores import store_session
from aios.datastores.models import AgentRun, Event, Invocation
from aios.db import SessionLocal
from aios.models import DataStore, EventType, SchemaChangeStatus
from aios.testing.fake_model import replies, use_model

MARKDOWN = b"# Field guide\n\nZebrafish embryos are transparent.\n"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Conversion (Docling, only in the worker image) as a stand-in: one chunk of the text."""

    def chunk_document(filename: str, data: bytes) -> list[dict]:
        text = data.decode()
        return [{"text": text, "contextualized": text, "meta": {"headings": [], "pages": []}}]

    monkeypatch.setattr(ingest, "chunk_document", chunk_document)


@pytest.fixture
def write_store(db, user) -> DataStore:
    """The signed-in user's store, with a `data.fish` table holding a pike."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute(
            "CREATE TABLE data.fish (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " name text NOT NULL UNIQUE)"
        )
        conn.execute("INSERT INTO data.fish (name) VALUES ('pike')")
    return row


apply_store = write_store


@pytest.fixture
def writer(api) -> dict:
    """An agent with the data tools."""
    agent = make_agent(api, "Writer")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def events(store_id) -> list[Event]:
    with store_session(store_id) as store:
        return list(store.scalars(select(Event).order_by(Event.id)))


def pending_at(store_id):
    with SessionLocal() as db:
        return db.scalar(select(DataStore.events_pending_at).where(DataStore.id == store_id))


def clear_pending(store_id) -> None:
    with SessionLocal() as db:
        db.execute(update(DataStore).where(DataStore.id == store_id).values(events_pending_at=None))
        db.commit()


def test_a_document_becoming_ready_writes_its_event_and_stamps_the_store(user, files_bucket):
    store_id = store_of(user)
    doc_id = add_document(store_id, MARKDOWN, filename="guide.md", content_type="text/markdown")
    clear_pending(store_id)

    with store_session(store_id) as store:
        ingest.ingest_document(store, doc_id)

    (event,) = events(store_id)
    assert event.type == EventType.document_ingested
    assert event.payload["document_id"] == doc_id
    assert event.payload["name"] == "guide.md"
    assert event.payload["content_type"] == "text/markdown"
    assert isinstance(event.payload["source_id"], int)
    # A member's upload: nothing caused it.
    assert event.invocation_id is None and event.chain == []
    assert event.dispatched_at is None
    assert pending_at(store_id) is not None


def test_a_document_that_fails_to_ingest_writes_no_event(user, files_bucket, monkeypatch):
    store_id = store_of(user)
    doc_id = add_document(store_id, MARKDOWN, filename="guide.md")
    clear_pending(store_id)

    def broken(*args, **kwargs):
        raise RuntimeError("bad file")

    monkeypatch.setattr(ingest, "chunk_document", broken)
    with store_session(store_id) as store, pytest.raises(RuntimeError):
        ingest.ingest_document(store, doc_id)

    assert events(store_id) == []
    assert pending_at(store_id) is None


def test_a_write_data_call_writes_its_event_caused_by_the_writing_run(api, write_store, writer):
    store_id = write_store.id
    clear_pending(store_id)

    result = write(
        api, writer, table="fish", operation="insert", rows=[{"name": "eel"}, {"name": "carp"}]
    )

    assert result["row_count"] == 2
    (event,) = events(store_id)
    assert event.type == EventType.table_written
    assert event.payload == {
        "table": "data.fish",
        "operation": "insert",
        "row_count": 2,
        "agent_id": writer["id"],
    }
    with store_session(store_id) as store:
        cause = store.get(Invocation, event.invocation_id)
        run = store.get(AgentRun, cause.agent_run_id)
    assert run.agent_id == writer["id"]
    assert event.chain == [cause.id]
    assert pending_at(store_id) is not None


def test_a_refused_write_writes_no_event(api, write_store, writer):
    result = write(
        api, writer, table="fish", operation="insert", rows=[{"name": "eel"}, {"name": "pike"}]
    )
    assert "error" in result
    assert events(write_store.id) == []


def test_an_applied_schema_change_writes_its_event(db, apply_store):
    request = propose(apply_store, CONTACTS, {"op": "drop_table", "table": "fish"})
    clear_pending(apply_store.id)

    approved = approve(db, request)

    assert approved.status == SchemaChangeStatus.applied
    (event,) = events(apply_store.id)
    assert event.type == EventType.schema_change_applied
    assert event.payload == {"request_id": str(request.id), "tables": ["contacts", "fish"]}
    # Asked for by no run here: nothing caused it but the member who approved it.
    assert event.invocation_id is None and event.chain == []
    assert pending_at(apply_store.id) is not None


def test_a_schema_change_that_fails_writes_no_event(db, apply_store):
    request = propose(apply_store, {"op": "drop_table", "table": "fish"})
    with as_owner(apply_store) as conn:
        conn.execute("DROP TABLE data.fish")  # Gone before it's applied: stale, or failed.

    approved = approve(db, request)

    assert approved.status != SchemaChangeStatus.applied
    assert events(apply_store.id) == []


def test_a_posted_message_writes_its_event(api, user):
    store_id = store_of(user)
    chat = make_chat(api, "Design")
    clear_pending(store_id)

    message = post(api, chat, "Hello")

    (event,) = events(store_id)
    assert event.type == EventType.chat_message
    assert event.payload == {
        "chat_id": chat["id"],
        "message_id": message["id"],
        "author_kind": "user",
        "author_id": user.id,
        "mentions": [],
    }
    assert event.invocation_id is None and event.chain == []
    assert pending_at(store_id) is not None


def test_a_refused_post_writes_no_event(api, user):
    chat = make_chat(api, "Design")
    resp = api.post(f"/api/chats/{chat['id']}/messages", json={"body": ""})
    assert resp.status_code == 422
    assert events(store_of(user)) == []


def test_an_agents_reply_in_a_chat_writes_its_event_caused_by_its_invocation(api, user):
    store_id = store_of(user)
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)

    with use_model(replies("Hi there.")):
        posted = say(api, chat, f"@[Bot](agent:{agent['id']}) hi", ("agent", agent))

    (invocation,) = posted["invocations"]
    by_type = [(e.type, e.payload["author_kind"], e.invocation_id) for e in events(store_id)]
    assert by_type == [
        (EventType.chat_message, "user", None),
        (EventType.chat_message, "agent", invocation["id"]),
    ]
    reply = events(store_id)[-1]
    assert reply.chain == [invocation["id"]]
    assert reply.payload["author_id"] == agent["id"]
