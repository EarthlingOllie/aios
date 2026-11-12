"""Phase 3's later events (`aios.outbox`, `aios.dispatcher`): table written, schema change
applied and chat message triggers and their filters, debounce batches, and listening to any
chat. Webhooks are in `test_webhooks.py`."""

import json

import pytest
from conftest import signed_in_store
from sqlalchemy import select, update
from test_chats import make_chat, other, post, signed_in_as  # noqa: F401
from test_event_dispatcher import dispatch, fired, shown
from test_event_outbox import (  # noqa: F401
    clear_pending,
    events,
    pending_at,
    write_store,
    writer,
)
from test_scheduler import make_runnable_workflow, queued  # noqa: F401
from test_schema_change_apply import CONTACTS, approve, propose
from test_triggers import make_agent, make_trigger
from test_write_data import write

from aios import dispatcher, jev, outbox
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import AgentRun, ChatMember, EventBatch, WorkflowRun
from aios.db import SessionLocal
from aios.models import DataStore, EventType
from aios.testing.fake_model import model

apply_store = write_store


def event_trigger(event_type: str, target_kind: str, target_id: int, **extra) -> dict:
    return {
        "kind": "event",
        "event_type": event_type,
        "target_kind": target_kind,
        "target_id": target_id,
        **extra,
    }


def emitted(type: EventType, payload: dict, invocation_id: int | None = None) -> int:
    """An event in the signed-in user's store, as its emitter would write it."""
    with store_session(signed_in_store()) as store:
        event_id = outbox.emit(store, type, payload, invocation_id=invocation_id)
        store.commit()
    return event_id


# --- Table written ----------------------------------------------------------------------------


def test_table_written_triggers_filter_by_table_and_operation(api, write_store, writer):  # noqa: F811
    reader = make_agent(api, "Reader")
    inserts = make_trigger(
        api,
        event_trigger(
            "table_written",
            "agent",
            reader["id"],
            filter={"tables": ["Data.Fish"], "operations": ["insert"]},
        ),
    )
    # Normalised: without `data.`, lower case.
    assert inserts["filter"] == {"tables": ["fish"], "operations": ["insert"]}
    deletes = make_trigger(
        api,
        event_trigger("table_written", "agent", reader["id"], filter={"operations": ["delete"]}),
    )
    birds = make_trigger(
        api, event_trigger("table_written", "agent", reader["id"], filter={"tables": ["birds"]})
    )
    any_write = make_trigger(api, event_trigger("table_written", "agent", reader["id"]))
    assert any_write["description"] == "When a data table is written"

    result = write(api, writer, table="fish", operation="insert", rows=[{"name": "eel"}])
    assert result["row_count"] == 1
    dispatch()

    assert len(fired(inserts["id"])) == 1
    assert len(fired(any_write["id"])) == 1
    assert fired(deletes["id"]) == [] and fired(birds["id"]) == []


def test_table_written_filters_refuse_unknown_operations(api):
    reader = make_agent(api, "Reader")
    resp = api.post(
        "/api/triggers",
        json=event_trigger(
            "table_written", "agent", reader["id"], filter={"operations": ["truncate"]}
        ),
    )
    assert resp.status_code == 422
    assert "insert" in resp.json()["detail"]


def test_an_agents_own_writes_dont_trigger_it_again(api, write_store, writer):  # noqa: F811
    own = make_trigger(
        api, event_trigger("table_written", "agent", writer["id"], filter={"tables": ["fish"]})
    )
    write(api, writer, table="fish", operation="insert", rows=[{"name": "eel"}])
    dispatch()
    assert fired(own["id"]) == []
    after = shown(api, own["id"])
    assert after["last_status"] == "refused" and "own target" in after["last_error"]


# --- Schema change applied --------------------------------------------------------------------


def test_schema_change_applied_triggers_filter_by_table(api, db, apply_store):
    reader = make_agent(api, "Reader")
    contacts = make_trigger(
        api,
        event_trigger(
            "schema_change_applied", "agent", reader["id"], filter={"tables": ["contacts"]}
        ),
    )
    birds = make_trigger(
        api,
        event_trigger("schema_change_applied", "agent", reader["id"], filter={"tables": ["birds"]}),
    )
    approve(db, propose(apply_store, CONTACTS))
    dispatch()
    assert len(fired(contacts["id"])) == 1
    assert fired(birds["id"]) == []


def test_schema_change_applied_triggers_take_no_operations(api):
    reader = make_agent(api, "Reader")
    resp = api.post(
        "/api/triggers",
        json=event_trigger(
            "schema_change_applied", "agent", reader["id"], filter={"operations": ["insert"]}
        ),
    )
    assert resp.status_code == 422


# --- Chat message -----------------------------------------------------------------------------


def chat_trigger(target_kind: str, target_id: int, chat_id: int, **filter) -> dict:
    return event_trigger(
        "chat_message", target_kind, target_id, filter={"chat_id": chat_id, **filter}
    )


def test_a_chat_message_trigger_needs_its_chat(api):
    reader = make_agent(api, "Reader")
    resp = api.post("/api/triggers", json=event_trigger("chat_message", "agent", reader["id"]))
    assert resp.status_code == 422
    assert "chat_id" in resp.json()["detail"]
    resp = api.post("/api/triggers", json=chat_trigger("agent", reader["id"], 999_999))
    assert resp.status_code == 422


def test_keywords_match_whole_words_ignoring_case(api):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    t = make_trigger(api, chat_trigger("agent", reader["id"], chat["id"], keywords=["Outage"]))
    post(api, chat, "Several outages today")
    post(api, chat, "No-outage-here? fine")
    post(api, chat, "We have an OUTAGE.")
    dispatch()
    told = [i.event_id for i in fired(t["id"])]
    assert len(told) == 2
    texts = [e.payload["text"] for e in events(signed_in_store()) if e.id in told]
    assert texts == ["No-outage-here? fine", "We have an OUTAGE."]


def test_chat_message_triggers_filter_by_chat_author_kind_and_mentions(api, user):
    reader = make_agent(api, "Reader")
    bot = make_agent(api, "Bot")
    ops = make_chat(api, "Ops")
    design = make_chat(api, "Design")
    in_ops = make_trigger(api, chat_trigger("agent", reader["id"], ops["id"]))
    by_agents = make_trigger(
        api, chat_trigger("agent", reader["id"], ops["id"], author_kinds=["agent"])
    )
    mentioning_bot = make_trigger(
        api, chat_trigger("agent", reader["id"], ops["id"], mentions=[f"agent:{bot['id']}"])
    )
    post(api, ops, "Hello")
    post(api, design, "Hello there")
    # An agent's post in the chat (as the post-message tool or a handback writes it).
    emitted(
        EventType.chat_message,
        {
            "chat_id": ops["id"],
            "message_id": 1,
            "author_kind": "agent",
            "author_id": bot["id"],
            "mentions": [{"kind": "agent", "id": bot["id"]}],
            "text": "Done",
        },
    )
    dispatch()
    assert len(fired(in_ops["id"])) == 2
    assert len(fired(by_agents["id"])) == 1
    assert len(fired(mentioning_bot["id"])) == 1


def test_bad_mentions_and_author_kinds_are_refused(api):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    for bad in ({"mentions": ["bot"]}, {"author_kinds": ["robot"]}, {"keywords": [3]}):
        resp = api.post(
            "/api/triggers", json=chat_trigger("agent", reader["id"], chat["id"], **bad)
        )
        assert resp.status_code == 422, bad


def chat_members(store, chat_id: int) -> set[tuple[str, int | None]]:
    rows = store.scalars(select(ChatMember).where(ChatMember.chat_id == chat_id)).all()
    return {(m.kind.value, m.user_id or m.agent_id or m.workflow_id) for m in rows}


def test_a_trigger_listens_to_and_delivers_to_a_chat_its_creator_isnt_in(
    api,
    user,
    in_store,
    other,  # noqa: F811
):
    reader = make_agent(api, "Reader")
    with signed_in_as(other):
        theirs = make_chat(api, "Theirs")
        elsewhere = make_chat(api, "Elsewhere")
    t = make_trigger(
        api, chat_trigger("agent", reader["id"], theirs["id"]) | {"deliver_to": elsewhere["id"]}
    )
    assert t["filter"] == {"chat_id": theirs["id"]}
    assert t["deliver_to"]["id"] == elsewhere["id"]
    # Listening adds its agent to the chat, as delivery does; the creator isn't added.
    assert ("agent", reader["id"]) in chat_members(in_store, theirs["id"])
    assert ("agent", reader["id"]) in chat_members(in_store, elsewhere["id"])
    assert ("user", user.id) not in chat_members(in_store, theirs["id"])

    with signed_in_as(other):
        post(api, theirs, "Can someone look at this?")
    dispatch()
    (invocation,) = fired(t["id"])
    assert in_store.get(AgentRun, invocation.agent_run_id) is not None


def test_a_workflow_listening_to_a_chat_is_added_to_it(api, in_store):
    workflow = make_runnable_workflow(api)
    chat = make_chat(api, "Ops")
    make_trigger(api, chat_trigger("workflow", workflow["id"], chat["id"]))
    assert ("workflow", workflow["id"]) in chat_members(in_store, chat["id"])


def test_an_agents_own_reply_in_a_chat_it_listens_to_doesnt_trigger_it(api, in_store):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    t = make_trigger(
        api, chat_trigger("agent", reader["id"], chat["id"]) | {"deliver_to": chat["id"]}
    )
    post(api, chat, "Hello")
    # Its run's result is delivered to the chat: a message its own run caused.
    dispatch()
    dispatch()
    assert len(fired(t["id"])) == 1
    assert "own target" in shown(api, t["id"])["last_error"]


# --- Debounce ---------------------------------------------------------------------------------


def close_batches(store) -> None:
    """Every open debounce batch's window over, as if its time had passed."""
    store.execute(update(EventBatch).values(closes_at=EventBatch.opened_at))


def test_with_a_debounce_window_thirty_matches_give_one_run(api, in_store):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    t = make_trigger(
        api,
        chat_trigger(
            "agent",
            reader["id"],
            chat["id"],
        )
        | {"debounce_seconds": 60},
    )
    assert t["debounce_seconds"] == 60
    seen = []

    async def script(messages, info):
        seen.append(messages[-1].content)
        yield "Read them."

    for i in range(30):
        post(api, chat, f"Message {i}")
    dispatch()
    # Gathered, not fired: the window is still open.
    assert fired(t["id"]) == []
    (batch,) = in_store.scalars(select(EventBatch)).all()
    assert len(batch.event_ids) == 30 and batch.dropped == 0

    close_batches(in_store)
    dispatch(model(script))
    (invocation,) = fired(t["id"])
    assert in_store.scalars(select(EventBatch)).all() == []
    # Told every event, in order, and that none were left out.
    told = json.loads(jev.unwrap(next(m for m in seen if jev.is_wrapped(m))))
    assert told["count"] == 30 and told["dropped"] == 0
    assert [e["payload"]["text"] for e in told["events"]] == [f"Message {i}" for i in range(30)]
    assert invocation.event_id == told["events"][-1]["id"]
    # Nothing more to fire.
    dispatch()
    assert len(fired(t["id"])) == 1


def test_a_batch_is_capped_with_a_dropped_count(api, in_store, queued, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "event_batch_cap", 10)
    workflow = make_runnable_workflow(api)
    chat = make_chat(api, "Ops")
    t = make_trigger(
        api,
        chat_trigger("workflow", workflow["id"], chat["id"])
        | {"debounce_seconds": 30, "input": "{{count}} kept, {{dropped}} dropped"},
    )
    for i in range(30):
        post(api, chat, f"Message {i}")
    dispatch()
    close_batches(in_store)
    dispatch()
    (invocation,) = fired(t["id"])
    run = in_store.get(WorkflowRun, invocation.workflow_run_id)
    assert run.input == "10 kept, 20 dropped"


def test_a_debounced_store_is_stamped_to_be_claimed_when_its_batch_closes(api, in_store):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    t = make_trigger(
        api, chat_trigger("agent", reader["id"], chat["id"]) | {"debounce_seconds": 60}
    )
    post(api, chat, "Hello")
    store_id = signed_in_store()
    dispatcher.drain_once()
    # Stamped for when its batch closes: not claimed before then.
    (batch,) = in_store.scalars(select(EventBatch)).all()
    assert pending_at(store_id) == batch.closes_at
    assert dispatcher.claim_pending() == []
    close_batches(in_store)
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == store_id)
            .values(events_pending_at=batch.opened_at)
        )
        db.commit()
    dispatcher.drain_once()
    assert len(fired(t["id"])) == 1


@pytest.mark.parametrize("seconds", [0, 86_401])
def test_a_debounce_window_is_a_second_to_a_day(api, seconds):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    resp = api.post(
        "/api/triggers",
        json=chat_trigger("agent", reader["id"], chat["id"]) | {"debounce_seconds": seconds},
    )
    assert resp.status_code == 422


def test_the_sweep_stamps_a_store_whose_open_batch_lost_its_stamp(api, in_store):
    reader = make_agent(api, "Reader")
    chat = make_chat(api, "Ops")
    make_trigger(api, chat_trigger("agent", reader["id"], chat["id"]) | {"debounce_seconds": 60})
    post(api, chat, "Hello")
    store_id = signed_in_store()
    dispatcher.drain_once(store_id)
    clear_pending(store_id)

    dispatcher.sweep()

    (batch,) = in_store.scalars(select(EventBatch)).all()
    assert pending_at(store_id) == batch.closes_at
