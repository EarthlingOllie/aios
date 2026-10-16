"""The run history export: copying a user's agent runs into their data store's `history` schema
(`aios.datastores.history`), switched on and off from the Settings page."""

import asyncio
import threading
import time
import uuid

import pytest
from conftest import delete_user
from fake_model import model, replies, use_model
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import text
from test_agents import chat_body, make_agent
from test_workflows import agent_node, make_workflow

from aios import worker
from aios.api import workflows as workflows_api
from aios.auth import current_user
from aios.datastores import DataStoreBusy, data_store_for, history, provision, store_session
from aios.db import SessionLocal
from aios.main import app
from aios.models import AgentRun, HistoryExport, User
from aios.workflows import execute_run


def settings(api) -> dict:
    response = api.get("/api/settings")
    assert response.status_code == 200
    return response.json()


def switch(api, on: bool) -> dict:
    response = api.put("/api/settings", json={"export_run_history": on})
    assert response.status_code == 200
    return response.json()


def stored(store_id: uuid.UUID, sql: str) -> list[tuple]:
    """Rows from the store, as its read-only role reads them (as `query_data` does)."""
    with store_session(store_id, "ro") as store:
        return [tuple(r) for r in store.execute(text(sql))]


def chat(api, agent: dict, run: dict, message: str, reply: str = "ok") -> None:
    with use_model(replies(reply)):
        response = api.post(
            f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body(message)
        )
    assert response.status_code == 200


def new_run(api, agent: dict) -> dict:
    return api.post(f"/api/agents/{agent['id']}/runs").json()


def set_up_and_export(store_id: uuid.UUID, history_tasks) -> None:
    """What the workers do once the export is switched on: the provisioning worker gives the
    store its `history` tables, then the export drains the outbox."""
    for queued in history_tasks["set_up"]:
        worker.set_up_history_export_task.apply(args=[str(queued)]).get()
    history_tasks["set_up"].clear()
    worker.export_history_task.apply(args=[str(store_id)]).get()


@pytest.fixture
def store_id(user) -> uuid.UUID:
    return data_store_for(user.id)


def test_the_export_is_off_until_switched_on_and_the_setting_persists(api, user):
    assert settings(api) == {
        "export_run_history": False,
        "history_exported_at": None,
        "history_ready": False,
    }

    assert switch(api, True)["export_run_history"] is True
    assert settings(api)["export_run_history"] is True
    with SessionLocal() as db:
        assert db.get(type(user), user.id).export_run_history is True

    assert switch(api, False)["export_run_history"] is False
    assert settings(api)["export_run_history"] is False


def test_switching_it_on_creates_the_history_tables_and_backfills_existing_runs(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hello there", "arr")
    other = new_run(api, agent)  # never chatted in

    switch(api, True)

    assert history_tasks["set_up"] == [store_id]
    set_up_and_export(store_id, history_tasks)
    assert settings(api)["history_ready"] is True
    assert settings(api)["history_exported_at"] is not None
    assert stored(
        store_id,
        "SELECT id, agent_id, agent_name, agent_version, title, user_id, message_count"
        " FROM history.agent_runs ORDER BY id",
    ) == [
        (run["id"], agent["id"], "Bot", 1, "hello there", user.id, 2),
        (other["id"], agent["id"], "Bot", 1, None, user.id, 0),
    ]
    assert stored(
        store_id, "SELECT agent_run_id, position, role, text FROM history.messages ORDER BY 2"
    ) == [(run["id"], 0, "user", "hello there"), (run["id"], 1, "assistant", "arr")]


def test_a_turn_that_ends_is_exported_shortly_after(api, user, store_id, history_tasks):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    history_tasks["export"].clear()

    chat(api, agent, run, "hi", "hello")

    # Its outbox row was written with the run's update, and the export queued once committed.
    assert history_tasks["export"] == [store_id]
    with SessionLocal() as db:
        row = db.query(HistoryExport).filter_by(source_id=run["id"]).one()
        assert row.exported_at is None
    worker.export_history_task.apply(args=[str(store_id)]).get()
    assert stored(store_id, "SELECT role, text FROM history.messages ORDER BY position") == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_exporting_again_gives_no_duplicates_and_catches_up_with_new_turns(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "one")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    # Run again, with nothing pending, and with the run re-queued (a backfill run twice).
    history.export(store_id)
    switch(api, True)
    history.export(store_id)
    with SessionLocal() as db:
        history.enqueue_runs(db, AgentRun.id == run["id"])
        db.commit()
    history.export(store_id)

    count = "SELECT count(*), count(DISTINCT (agent_run_id, position)) FROM history.messages"
    assert stored(store_id, count) == [(2, 2)]
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(1,)]

    chat(api, agent, run, "two")
    history.export(store_id)
    assert stored(store_id, count) == [(4, 4)]
    assert stored(store_id, "SELECT message_count FROM history.agent_runs") == [(4,)]


def test_a_turn_that_fails_exports_what_the_user_saw(api, user, store_id, history_tasks):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)

    async def fails_midway(messages, info):
        yield "half an "
        raise RuntimeError("model fell over")

    with use_model(model(fails_midway)):
        api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hello"))
    history.export(store_id)

    assert stored(store_id, "SELECT role, text FROM history.messages ORDER BY position") == [
        ("user", "hello"),
        ("assistant", "half an "),
    ]


def test_deleting_a_run_or_its_agent_deletes_its_copy(api, user, store_id, history_tasks):
    agent = make_agent(api, "Bot")
    runs = [new_run(api, agent) for _ in range(2)]
    for run in runs:
        chat(api, agent, run, "hi")
    other_agent = make_agent(api, "Other")
    kept = new_run(api, other_agent)
    chat(api, other_agent, kept, "keep me")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(3,)]

    assert api.delete(f"/api/agents/{agent['id']}/runs/{runs[0]['id']}").status_code == 204
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.agent_runs ORDER BY id") == [
        (runs[1]["id"],),
        (kept["id"],),
    ]

    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.agent_runs") == [(kept["id"],)]
    assert stored(store_id, "SELECT DISTINCT agent_run_id FROM history.messages") == [(kept["id"],)]


def test_switching_it_off_stops_the_export_and_keeps_what_was_exported(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "before")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)

    switch(api, False)
    history_tasks["export"].clear()
    chat(api, agent, run, "after")
    later = new_run(api, agent)
    chat(api, agent, later, "unseen")
    assert api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code == 204
    history.export(store_id)

    assert history_tasks["export"] == []
    assert stored(store_id, "SELECT id, message_count FROM history.agent_runs") == [(run["id"], 2)]
    assert settings(api)["history_ready"] is True


def test_delete_exported_history_empties_it(api, user, store_id, history_tasks):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    switch(api, False)

    assert api.delete("/api/settings/exported-history").status_code == 204

    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    assert stored(store_id, "SELECT count(*) FROM history.messages") == [(0,)]
    # Nothing to delete is no error, before the history tables exist too.
    assert api.delete("/api/settings/exported-history").status_code == 204


def test_delete_exported_history_before_the_export_ever_ran(api, user):
    assert api.delete("/api/settings/exported-history").status_code == 204


@pytest.fixture
def export_running(api, user, store_id, history_tasks, monkeypatch):
    """An export of the store under way (e.g. a long backfill), stuck reading a run's thread
    until the test ends; 5 seconds at most, so a test that waits for it fails rather than hangs."""
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "hi")
    switch(api, True)
    for queued in history_tasks["set_up"]:
        provision.migrate_store(queued)
    reading, release = threading.Event(), threading.Event()
    threading.Timer(5, release.set).start()
    read = history.checkpoints.history

    def slow(run_id):
        reading.set()
        release.wait()
        return read(run_id)

    monkeypatch.setattr(history.checkpoints, "history", slow)
    running = threading.Thread(target=history.export, args=[store_id])
    running.start()
    assert reading.wait(5)
    yield
    release.set()
    running.join()


def test_an_export_of_a_store_already_exporting_leaves_it_to_that_one(
    api, user, store_id, export_running
):
    # It doesn't hold a worker waiting: what's still pending after, the sweep queues again.
    started = time.monotonic()
    assert history.export(store_id) == 0
    assert time.monotonic() - started < 1


def test_delete_exported_history_while_an_export_is_running_is_busy(
    api, user, store_id, export_running, monkeypatch
):
    monkeypatch.setattr(history, "LOCK_WAIT", 0.2)

    started = time.monotonic()
    response = api.delete("/api/settings/exported-history")

    assert response.status_code == 503
    assert time.monotonic() - started < 2


def test_the_store_roles_may_read_the_history_tables_and_only_rw_write_them(
    api, user, store_id, history_tasks
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    with store_session(store_id, "ro") as store, pytest.raises(Exception, match="permission"):
        store.execute(text("DELETE FROM history.messages"))
    with store_session(store_id, "rw") as store, pytest.raises(Exception, match="permission"):
        store.execute(text("CREATE TABLE history.mine (id int)"))


def test_a_workflow_runs_agent_turns_are_exported_and_go_with_the_workflow_run(
    api, user, store_id, history_tasks, monkeypatch
):
    queued = []
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", queued.append)
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Echo")
    workflow = make_workflow(api, "flow", [agent_node("one", agent)])
    started = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"}).json()
    history_tasks["export"].clear()

    with use_model(replies("went")):
        asyncio.run(execute_run(started["id"]))

    assert history_tasks["export"] == [store_id]
    history.export(store_id)
    assert stored(
        store_id, "SELECT workflow_run_id, user_id, title, message_count FROM history.agent_runs"
    ) == [(started["id"], user.id, "go", 2)]

    url = f"/api/workflows/{workflow['id']}/runs/{started['id']}"
    assert api.delete(url).status_code == 204
    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]


def test_messages_are_flattened_to_role_text_tool_calls_and_usage():
    messages = [
        HumanMessage("what's 2+2?", id="h1"),
        AIMessage(
            [{"type": "text", "text": "Let me check."}],
            id="a1",
            tool_calls=[{"id": "call_1", "name": "run_python", "args": {"code": "2+2"}}],
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": "claude-x"},
        ),
        ToolMessage("boom\x00", tool_call_id="call_1", name="run_python", status="error"),
        AIMessage("4"),
    ]

    rows = history.flatten(7, messages)

    assert [
        (r["agent_run_id"], r["position"], r["role"], r["text"], r["message_id"]) for r in rows
    ] == [
        (7, 0, "user", "what's 2+2?", "h1"),
        (7, 1, "assistant", "Let me check.", "a1"),
        (7, 2, "tool", "boom", None),
        (7, 3, "assistant", "4", None),
    ]
    assert rows[1]["tool_calls"] == [
        {"id": "call_1", "name": "run_python", "args": {"code": "2+2"}}
    ]
    assert rows[1]["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert rows[1]["model"] == "claude-x"
    assert (rows[2]["tool_call_id"], rows[2]["tool_name"], rows[2]["is_error"]) == (
        "call_1",
        "run_python",
        True,
    )
    assert rows[3]["tool_calls"] is None and rows[0]["usage"] is None


def test_a_change_that_is_rolled_back_queues_no_export(api, user, store_id, history_tasks):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    switch(api, True)
    history_tasks["export"].clear()

    with SessionLocal() as db:
        history.enqueue_runs(db, AgentRun.id == run["id"])
        db.rollback()
        db.commit()

    assert history_tasks["export"] == []


def test_the_sweep_queues_exporting_stores_with_runs_still_waiting(
    api, user, store_id, history_tasks, monkeypatch
):
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "hi")
    switch(api, True)
    swept = []
    monkeypatch.setattr(history, "enqueue_export", swept.append)

    # Not set up yet: its rows wait.
    worker.export_pending_history_task.apply().get()
    assert store_id not in swept

    for queued in history_tasks["set_up"]:
        provision.migrate_store(queued)
    worker.export_pending_history_task.apply().get()
    assert swept.count(store_id) == 1

    swept.clear()
    history.export(store_id)
    worker.export_pending_history_task.apply().get()
    assert store_id not in swept
    beat = worker.celery_app.conf.beat_schedule["export-pending-history"]
    assert beat["task"] == "aios.export_pending_history"


def test_the_export_task_tries_a_busy_store_again(user, store_id, monkeypatch):
    def busy(data_store_id):
        raise DataStoreBusy(data_store_id)

    monkeypatch.setattr(history, "export", busy)
    retried = []

    def retry(*args, **kwargs):
        retried.append(kwargs.get("countdown"))
        raise RuntimeError("retried")

    monkeypatch.setattr(worker.export_history_task, "retry", retry)

    result = worker.export_history_task.apply(args=[str(store_id)])

    assert result.failed() and retried == [worker.BUSY_RETRY_COUNTDOWN]


def test_switching_it_on_before_the_store_is_ready_sets_it_up_when_provisioned(
    client, db, history_tasks
):
    u = User(github_id=-77, login="unready")
    db.add(u)
    db.commit()
    app.dependency_overrides[current_user] = lambda: u
    try:
        out = client.put("/api/settings", json={"export_run_history": True})
        assert out.status_code == 200
        assert out.json()["history_ready"] is False
        store_id = data_store_for(u.id)
        assert history_tasks["set_up"] == [store_id]

        provision.provision(store_id)

        assert client.get("/api/settings").json()["history_ready"] is True
    finally:
        app.dependency_overrides.clear()
        delete_user(db, u)


def test_a_run_that_cant_be_exported_is_set_aside_with_its_error_and_the_rest_go_in(
    api, user, store_id, history_tasks, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    broken, fine = new_run(api, agent), new_run(api, agent)
    chat(api, agent, broken, "first")
    # Its checkpoint no longer loads (not valid msgpack), so it can't be flattened.
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE langgraph.checkpoint_blobs SET blob = '\\xc1'::bytea"
                " WHERE thread_id = :thread AND channel = 'messages'"
            ),
            {"thread": f"agent-run-{broken['id']}"},
        )
        db.commit()
    chat(api, agent, fine, "second", "hello")

    # The broken run is first in line; the task still succeeds and the next run goes in.
    assert worker.export_history_task.apply(args=[str(store_id)]).successful()
    assert stored(store_id, "SELECT id FROM history.agent_runs") == [(fine["id"],)]
    with SessionLocal() as db:
        row = db.query(HistoryExport).filter_by(source_id=broken["id"]).one()
        assert row.exported_at is None and row.error
    # Set aside: the sweep doesn't queue it again and again.
    swept = []
    monkeypatch.setattr(history, "enqueue_export", swept.append)
    worker.export_pending_history_task.apply().get()
    assert swept == []

    # Until the run changes again.
    with SessionLocal() as db:
        history.enqueue_runs(db, AgentRun.id == broken["id"])
        db.commit()
        row = db.query(HistoryExport).filter_by(source_id=broken["id"]).one()
        assert row.error is None
    assert history.stores_to_export() == [store_id]
