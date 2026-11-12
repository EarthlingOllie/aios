"""The event dispatcher (`aios.dispatcher`), through its "drain once" entry point: what fires,
on what, and what the loop rules refuse."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from conftest import add_document, signed_in_store
from sqlalchemy import select, update
from test_audit_log import entries
from test_event_outbox import MARKDOWN, clear_pending, events, offline, pending_at  # noqa: F401
from test_event_triggers import document_trigger
from test_scheduler import make_runnable_workflow, queued  # noqa: F401
from test_triggers import make_agent, make_trigger

from aios import dispatcher, ingest, jev, outbox, worker
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Agent, AgentRun, Event, Invocation, WorkflowRun
from aios.models import EventType, InvocationCause
from aios.run_service import executor
from aios.testing.fake_model import model, replies, use_model


def ingested(name: str = "guide.md", content_type: str | None = "text/markdown", **values) -> int:
    """A document uploaded and ingested in the signed-in user's store: its id."""
    store_id = signed_in_store()
    doc_id = add_document(store_id, MARKDOWN, filename=name, content_type=content_type, **values)
    with store_session(store_id) as store:
        ingest.ingest_document(store, doc_id)
    return doc_id


def emitted(invocation_id: int | None = None, **payload) -> int:
    """A document ingested event in the signed-in user's store, caused by the invocation."""
    with store_session(signed_in_store()) as store:
        event_id = outbox.emit(
            store,
            EventType.document_ingested,
            {"document_id": 1, "name": "x.md", "content_type": "text/markdown", **payload},
            invocation_id=invocation_id,
        )
        store.commit()
    return event_id


def dispatch(script=None) -> int:
    """Drain the signed-in user's store's events, then run the turns that enqueued; how many
    events were dispatched."""
    count = dispatcher.drain_once(signed_in_store())
    with use_model(script or replies("Done.")):
        asyncio.run(executor.drain_once())
    return count


def fired(trigger_id: int) -> list[Invocation]:
    with store_session(signed_in_store()) as store:
        return list(
            store.scalars(
                select(Invocation)
                .where(Invocation.trigger_id == trigger_id)
                .order_by(Invocation.id)
            )
        )


def shown(api, trigger_id: int) -> dict:
    resp = api.get(f"/api/triggers/{trigger_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_ready_document_fires_each_matching_trigger_once(api, in_store):
    reader = make_agent(api, "Reader")
    filer = make_agent(api, "Filer")
    t = make_trigger(api, document_trigger("agent", reader["id"], input="Summarise it."))
    other = make_trigger(api, document_trigger("agent", filer["id"]))
    seen = []

    async def script(messages, info):
        seen.append(messages[-1].content)
        yield "Summarised."

    doc_id = ingested()
    assert dispatch(model(script)) == 1

    (invocation,) = fired(t["id"])
    (event,) = events(signed_in_store())
    assert event.dispatched_at is not None
    assert invocation.cause == InvocationCause.event
    assert invocation.event_id == event.id
    assert invocation.ancestor_ids == [] and invocation.depth == 0
    run = in_store.get(AgentRun, invocation.agent_run_id)
    # A one-turn run of the agent's current version, started by no member.
    assert run.agent_version_id == reader["current_version"]["id"]
    assert run.created_by is None and run.trigger_id == t["id"]
    assert run.message_count == 2
    after = shown(api, t["id"])
    assert (after["last_status"], after["last_error"]) == ("succeeded", None)
    assert after["last_run_id"] == run.id
    assert len(fired(other["id"])) == 1

    # Told the trigger's input, then the event, wrapped as untrusted.
    message = next(m for m in seen if m.startswith("Summarise it."))
    told = json.loads(jev.unwrap(message.removeprefix("Summarise it.\n\n")))
    assert told["type"] == "document_ingested"
    assert told["payload"]["document_id"] == doc_id
    assert told["payload"]["name"] == "guide.md"

    # Dispatched: draining again fires nothing.
    assert dispatch() == 0
    assert len(fired(t["id"])) == 1


def test_filters_by_source_and_content_type(api, in_store):
    agent = make_agent(api, "Reader")
    pdfs = make_trigger(
        api, document_trigger("agent", agent["id"], filter={"content_types": ["application/pdf"]})
    )
    text = make_trigger(
        api, document_trigger("agent", agent["id"], filter={"content_types": ["text/*"]})
    )
    elsewhere = make_trigger(api, document_trigger("agent", agent["id"], filter={"sources": [-1]}))
    ingested("guide.md", "text/markdown; charset=utf-8")
    with store_session(signed_in_store()) as store:
        (source_id,) = store.scalars(select(Event.payload["source_id"].as_integer())).all()
    uploads = make_trigger(
        api, document_trigger("agent", agent["id"], filter={"sources": [source_id]})
    )
    dispatch()
    assert len(fired(text["id"])) == 1
    assert len(fired(uploads["id"])) == 1
    assert fired(pdfs["id"]) == [] and fired(elsewhere["id"]) == []


def test_a_disabled_trigger_doesnt_fire(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"], enabled=False))
    ingested()
    dispatch()
    assert fired(t["id"]) == []


def test_a_workflow_target_gets_its_template_rendered_with_the_event(api, in_store, queued):  # noqa: F811
    workflow = make_runnable_workflow(api)
    t = make_trigger(
        api,
        document_trigger(
            "workflow", workflow["id"], input="File {{payload.name}} ({{ payload.document_id }})"
        ),
    )
    doc_id = ingested("report.pdf", "application/pdf")
    dispatch()
    (invocation,) = fired(t["id"])
    assert invocation.cause == InvocationCause.event
    run = in_store.get(WorkflowRun, invocation.workflow_run_id)
    assert run.workflow_version_id == workflow["current_version"]["id"]
    assert run.input == f"File report.pdf ({doc_id})"
    assert queued == [run.id]
    assert shown(api, t["id"])["last_workflow_run_id"] == run.id


def test_a_workflow_target_without_a_template_gets_the_event(api, in_store, queued):  # noqa: F811
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, document_trigger("workflow", workflow["id"], input=""))
    doc_id = ingested()
    dispatch()
    (invocation,) = fired(t["id"])
    run = in_store.get(WorkflowRun, invocation.workflow_run_id)
    given = json.loads(run.input)
    assert given["type"] == "document_ingested"
    assert given["payload"]["document_id"] == doc_id


def test_a_target_that_cant_run_fails_the_firing_without_a_run(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    # Archived behind the trigger's back (archiving pauses its triggers).
    in_store.execute(
        update(Agent).where(Agent.id == agent["id"]).values(archived_at=datetime.now(UTC))
    )
    ingested()
    dispatch()
    assert fired(t["id"]) == []
    after = shown(api, t["id"])
    assert after["last_status"] == "failed" and "archived" in after["last_error"]
    # The event is dispatched all the same.
    assert all(e.dispatched_at for e in events(signed_in_store()))


def started_by_hand(api, agent: dict) -> Invocation:
    """A run of the agent a member started by hand: its invocation."""
    resp = api.post(f"/api/agents/{agent['id']}/runs")
    assert resp.status_code == 201, resp.text
    with store_session(signed_in_store()) as store:
        return store.scalars(
            select(Invocation).where(Invocation.agent_run_id == resp.json()["id"])
        ).one()


def test_own_events_are_refused_unless_allowed(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    # The agent's own run (one a member started) causes a document: its trigger doesn't fire.
    own = started_by_hand(api, agent)
    emitted(own.id)
    dispatch()
    assert fired(t["id"]) == []
    after = shown(api, t["id"])
    assert after["last_status"] == "refused"
    assert "own target" in after["last_error"]
    (entry,) = entries(api, action="trigger.refused")
    assert entry["target_id"] == str(t["id"]) and entry["actor"]["kind"] == "system"

    resp = api.put(
        f"/api/triggers/{t['id']}",
        json=document_trigger("agent", agent["id"], fire_on_own_events=True),
    )
    assert resp.status_code == 200, resp.text
    emitted(own.id)
    dispatch()
    (invocation,) = fired(t["id"])
    # It continues the event's chain.
    assert invocation.ancestor_ids == [own.id] and invocation.depth == 1


def test_a_two_trigger_cycle_is_cut_at_its_second_pass(api, in_store):
    a = make_agent(api, "A")
    b = make_agent(api, "B")
    to_a = make_trigger(api, document_trigger("agent", a["id"]))
    to_b = make_trigger(api, document_trigger("agent", b["id"]))
    # A member's upload fires both; follow A's: its run causes an event, which fires B (A's
    # own trigger refuses its own event).
    emitted()
    dispatch()
    (first_a,) = fired(to_a["id"])
    emitted(first_a.id)
    dispatch()
    b_from_a = fired(to_b["id"])[-1]
    assert b_from_a.ancestor_ids == [first_a.id] and b_from_a.depth == 1
    # B's run causes an event: A's trigger is already in its chain, so the cycle stops.
    emitted(b_from_a.id)
    dispatch()
    assert [i.id for i in fired(to_a["id"])] == [first_a.id]
    after = shown(api, to_a["id"])
    assert after["last_status"] == "refused" and "chain" in after["last_error"]
    # And B's trigger refuses its own target's event.
    assert len(fired(to_b["id"])) == 2


def test_a_chain_past_the_maximum_depth_is_refused(api, in_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "chain_max_depth", 2)
    a = make_agent(api, "A")
    b = make_agent(api, "B")
    c = make_agent(api, "C")
    to_a = make_trigger(api, document_trigger("agent", a["id"]))
    to_b = make_trigger(api, document_trigger("agent", b["id"]))
    to_c = make_trigger(api, document_trigger("agent", c["id"]))
    emitted()
    dispatch()
    # B's run causes an event: A and C fire, second in the chain.
    emitted(fired(to_b["id"])[0].id)
    dispatch()
    second = fired(to_c["id"])[-1]
    assert second.depth == 1 and len(fired(to_a["id"])) == 2
    # C's run causes one: A would be third in the chain, past 2.
    emitted(second.id)
    dispatch()
    assert len(fired(to_a["id"])) == 2
    after = shown(api, to_a["id"])
    assert after["last_status"] == "refused" and "longer than 2" in after["last_error"]


def test_a_trigger_past_its_rate_shows_rate_limited(api, in_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "event_trigger_rate_per_minute", 2)
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    for _ in range(3):
        emitted()
    dispatch()
    assert len(fired(t["id"])) == 2
    after = shown(api, t["id"])
    assert after["last_status"] == "rate_limited"
    assert after["last_error"].startswith("Rate limited")
    (entry,) = entries(api, action="trigger.refused")
    assert entry["target_id"] == str(t["id"])


def test_drain_once_dispatches_every_stamped_store(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    store_id = signed_in_store()
    ingested()
    assert pending_at(store_id) is not None
    dispatcher.drain_once()
    # Claimed: its stamp is cleared, and its event dispatched.
    assert pending_at(store_id) is None
    assert len(fired(t["id"])) == 1


def test_the_sweep_recovers_a_lost_stamp_and_prunes_old_events(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    store_id = signed_in_store()
    old = emitted()
    dispatch()
    in_store.execute(
        update(Event)
        .where(Event.id == old)
        .values(dispatched_at=datetime.now(UTC) - timedelta(days=15))
    )
    new = emitted()
    # Its stamp lost, as if the writer stopped between committing and stamping.
    clear_pending(store_id)

    swept = dispatcher.sweep()

    assert store_id in swept.stamped
    assert pending_at(store_id) is not None
    assert [e.id for e in events(store_id)] == [new]
    dispatcher.drain_once()
    assert len(fired(t["id"])) == 2


def test_an_event_from_a_rolled_back_change_is_never_dispatched(api, in_store):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    with store_session(signed_in_store()) as store:
        outbox.emit(store, EventType.document_ingested, {"document_id": 1})
        store.rollback()
    assert events(signed_in_store()) == []
    dispatch()
    assert fired(t["id"]) == []


def test_a_wake_up_brings_the_dispatcher_to_a_store_with_events(api, in_store, monkeypatch):
    # Only the wake-up, not its poll, could bring it in time.
    monkeypatch.setattr(get_settings(), "event_dispatch_poll_seconds", 600)
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))

    async def main() -> bool:
        dispatching = asyncio.create_task(dispatcher.run_forever())
        try:
            await asyncio.sleep(1)  # Listening by now.
            await asyncio.to_thread(ingested)
            for _ in range(100):
                if await asyncio.to_thread(fired, t["id"]):
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            dispatching.cancel()
            await asyncio.gather(dispatching, return_exceptions=True)

    assert asyncio.run(main())


def test_the_sweep_is_on_the_beat():
    entry = worker.celery_app.conf.beat_schedule["sweep-events"]
    assert entry["task"] == "aios.sweep_events"
    assert entry["schedule"] == get_settings().event_sweep_interval_seconds == 60


def test_a_refusal_after_a_workflow_run_is_what_the_trigger_shows(
    api,
    in_store,
    queued,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "event_trigger_rate_per_minute", 1)
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, document_trigger("workflow", workflow["id"], input=""))
    emitted()
    emitted()
    dispatch()
    assert len(queued) == 1
    after = shown(api, t["id"])
    assert (after["last_status"], after["last_workflow_run_id"]) == ("rate_limited", None)
