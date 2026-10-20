"""The run history export: copying a user's agent runs into their data store's `history` schema
(`aios.datastores.history`), switched on and off from the Settings page."""

import asyncio
import threading
import time
import uuid

import pytest
import test_workflow_runs as workflow_runs
from conftest import delete_user, make_user, org_of, store_of
from fake_model import model, replies, use_model
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import text, update
from test_agents import chat_body, make_agent, stop_during_searches
from test_workflows import agent_node, make_workflow, workflow_node

from aios import organisations, worker, workflows
from aios.api import agents as agents_api
from aios.api import workflows as workflows_api
from aios.auth import current_user
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    history,
    migrations,
    provision,
    store_session,
)
from aios.db import SessionLocal
from aios.main import app
from aios.models import AgentRun, DataStore, HistoryExport, WorkflowRun
from aios.workflows import execute_run


def settings(api) -> dict:
    response = api.get("/api/organisation/settings")
    assert response.status_code == 200
    return response.json()


def switch(api, on: bool) -> dict:
    response = api.put("/api/organisation/settings", json={"export_run_history": on})
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
    return store_of(user)


def test_the_export_is_off_until_switched_on_and_the_setting_persists_on_the_store(api, store_id):
    assert settings(api) == {
        "export_run_history": False,
        "history_exported_at": None,
        "history_ready": False,
        "embed_run_history": False,
        "history_embedded_at": None,
        "history_embed_error": None,
    }

    assert switch(api, True)["export_run_history"] is True
    assert settings(api)["export_run_history"] is True
    with SessionLocal() as db:
        assert db.get(DataStore, store_id).export_run_history is True

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


def test_with_no_runs_to_export_it_still_catches_up(api, user, store_id, history_tasks):
    # The Settings page waits for `history_exported_at` once the export is on.
    switch(api, True)
    set_up_and_export(store_id, history_tasks)

    assert settings(api)["history_ready"] is True
    assert settings(api)["history_exported_at"] is not None


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


def test_a_turn_that_is_stopped_exports_what_the_user_saw(api, user, db, store_id, history_tasks):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)

    async def slow(messages, info):
        yield "partial "
        await asyncio.sleep(60)
        yield "never sent"

    async def stop_after_first_text():
        # Stop in the UI aborts the fetch: the server sees its response task cancelled.
        response = await agents_api.run_chat(
            agent["id"], run["id"], chat_body("stop me"), user, db, org_of(db, user)
        )
        got_text = asyncio.Event()

        async def consume():
            async for chunk in response.body_iterator:
                if "partial" in chunk:
                    got_text.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(got_text.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with use_model(model(slow)):
        asyncio.run(stop_after_first_text())
    history.export(store_id)

    assert stored(store_id, "SELECT role, text FROM history.messages ORDER BY position") == [
        ("user", "stop me"),
        ("assistant", "partial "),
    ]
    assert stored(store_id, "SELECT message_count FROM history.agent_runs") == [(2,)]


def test_a_turn_stopped_during_tool_calls_exports_them_and_their_results(
    api, user, db, store_id, history_tasks, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)

    stop_during_searches(api, user, db, monkeypatch, [])
    history.export(store_id)

    rows = stored(
        store_id,
        "SELECT role, tool_calls, tool_call_id, is_error FROM history.messages ORDER BY position",
    )
    assert [(role, call_id, is_error) for role, _, call_id, is_error in rows] == [
        ("user", None, None),
        ("assistant", None, None),
        ("tool", "call-quick", False),
        # Still running when the turn stopped: the user saw it fail.
        ("tool", "call-slow", True),
    ]
    assert [c["id"] for c in rows[1][1]] == ["call-slow", "call-quick"]


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


def test_a_run_deleted_while_it_is_off_loses_its_copy_once_it_is_back_on(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    runs = [new_run(api, agent) for _ in range(2)]
    for run in runs:
        chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    switch(api, False)
    assert api.delete(f"/api/agents/{agent['id']}/runs/{runs[0]['id']}").status_code == 204
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(2,)]

    switch(api, True)
    set_up_and_export(store_id, history_tasks)

    assert stored(store_id, "SELECT id FROM history.agent_runs") == [(runs[1]["id"],)]
    assert stored(store_id, "SELECT DISTINCT agent_run_id FROM history.messages") == [
        (runs[1]["id"],)
    ]


def test_delete_exported_history_empties_it(api, user, store_id, history_tasks):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    switch(api, False)

    assert api.delete("/api/organisation/settings/exported-history").status_code == 204

    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    assert stored(store_id, "SELECT count(*) FROM history.messages") == [(0,)]
    # Nothing to delete is no error, before the history tables exist too.
    assert api.delete("/api/organisation/settings/exported-history").status_code == 204


def test_delete_exported_history_while_exporting_starts_the_copy_over(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    runs = [new_run(api, agent) for _ in range(2)]
    for run in runs:
        chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    history_tasks["export"].clear()

    assert api.delete("/api/organisation/settings/exported-history").status_code == 204

    # Emptied, and nothing is said to have been exported since.
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    assert settings(api)["history_exported_at"] is None
    # Every run is copied again, not just those with a turn since.
    assert history_tasks["export"] == [store_id]
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.agent_runs ORDER BY id") == [
        (run["id"],) for run in runs
    ]
    assert settings(api)["history_exported_at"] is not None


def test_delete_exported_history_while_off_keeps_it_empty(api, user, store_id, history_tasks):
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    switch(api, False)
    history_tasks["export"].clear()

    assert api.delete("/api/organisation/settings/exported-history").status_code == 204
    history.export(store_id)

    assert history_tasks["export"] == []
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    assert settings(api)["history_exported_at"] is None


def test_delete_exported_history_before_the_export_ever_ran(api, user):
    assert api.delete("/api/organisation/settings/exported-history").status_code == 204


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

    def slow(run_id, *args):
        reading.set()
        release.wait()
        return read(run_id, *args)

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
    response = api.delete("/api/organisation/settings/exported-history")

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

    assert set(history_tasks["export"]) == {store_id}
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


@pytest.mark.parametrize(
    "error",
    [DataStoreBusy, lambda store_id: DataStoreBehind(store_id, "h0001")],
    ids=["busy", "behind"],
)
def test_the_export_task_tries_a_busy_or_behind_store_again(user, store_id, monkeypatch, error):
    def busy(data_store_id):
        raise error(data_store_id)

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
    u = make_user(db, "unready")
    app.dependency_overrides[current_user] = lambda: u
    try:
        out = client.put("/api/organisation/settings", json={"export_run_history": True})
        assert out.status_code == 200
        assert out.json()["history_ready"] is False
        store_id = store_of(u)
        assert history_tasks["set_up"] == [store_id]

        provision.provision(store_id)

        assert client.get("/api/organisation/settings").json()["history_ready"] is True
        # With no runs to export, only the sweep exports it: then it has caught up.
        worker.export_pending_history_task.apply().get()
        assert history_tasks["export"] == [store_id]
        history.export(store_id)
        assert client.get("/api/organisation/settings").json()["history_exported_at"] is not None
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


def test_a_run_deleted_as_its_turn_ends_doesnt_deadlock(api, user, store_id, history_tasks):
    """Deleting a run writes its outbox row, then deletes it; a turn ending writes both too. In
    the same order, or each holds the row the other waits for."""
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    errors = []

    def end_turn():
        try:
            agents_api._end_turn(run["id"], failed=False, message_count=2)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with SessionLocal() as deleting:
        # delete_run, up to deleting the run: its outbox row is written (and locked).
        history.enqueue_runs(deleting, AgentRun.id == run["id"], deleting=True)
        ending = threading.Thread(target=end_turn)
        ending.start()
        time.sleep(0.5)  # it's waiting on the outbox row, or holding the run's
        try:
            deleting.delete(deleting.get(AgentRun, run["id"]))
            deleting.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        ending.join(10)

    assert errors == []
    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]


def test_a_run_set_aside_by_an_error_is_tried_again_later(
    api, user, store_id, history_tasks, monkeypatch
):
    """A run that will have no other turn (a backfilled one, say) isn't left out for good by an
    error that passes (a statement timeout, a dropped connection)."""
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hi")
    switch(api, True)
    read = history.checkpoints.history

    def fails(run_id, *args):
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(history.checkpoints, "history", fails)
    set_up_and_export(store_id, history_tasks)
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(0,)]
    assert history.stores_to_export() == []
    monkeypatch.setattr(history.checkpoints, "history", read)

    # Once its retry is due, the sweep queues the store and the run goes in.
    with SessionLocal() as db:
        db.execute(text("UPDATE history_exports SET retry_at = now() - interval '1 second'"))
        db.commit()
    assert history.stores_to_export() == [store_id]
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.agent_runs") == [(run["id"],)]
    with SessionLocal() as db:
        row = db.query(HistoryExport).filter_by(source_id=run["id"]).one()
        assert (row.error, row.retry_at) == (None, None) and row.exported_at is not None


def test_switching_it_back_on_says_its_exporting_until_the_backfill_is_done(
    api, user, store_id, history_tasks
):
    agent = make_agent(api, "Bot")
    chat(api, agent, new_run(api, agent), "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    switch(api, False)

    # Not a date from before: the Settings page waits for the backfill.
    assert switch(api, True)["history_exported_at"] is None
    set_up_and_export(store_id, history_tasks)
    assert settings(api)["history_exported_at"] is not None


def test_the_export_delete_exported_history_queues_can_start_at_once(
    api, user, store_id, history_tasks, monkeypatch
):
    agent = make_agent(api, "Bot")
    run = new_run(api, agent)
    chat(api, agent, run, "hi")
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    # A worker that picks the export up the moment it's queued.
    monkeypatch.setattr(history, "enqueue_export", history.export)

    assert api.delete("/api/organisation/settings/exported-history").status_code == 204

    assert stored(store_id, "SELECT id FROM history.agent_runs") == [(run["id"],)]


def test_switching_it_off_stops_an_export_under_way(
    api, user, store_id, history_tasks, monkeypatch
):
    agent = make_agent(api, "Bot")
    runs = [new_run(api, agent) for _ in range(3)]
    for run in runs:
        chat(api, agent, run, "hi")
    switch(api, True)
    for queued in history_tasks["set_up"]:
        provision.migrate_store(queued)
    monkeypatch.setattr(history, "BATCH", 1)
    read = history.checkpoints.history

    def switched_off_while_reading(run_id, *args):
        switch(api, False)
        return read(run_id, *args)

    monkeypatch.setattr(history.checkpoints, "history", switched_off_while_reading)

    # The batch under way finishes; no other starts.
    assert history.export(store_id) == 1
    assert stored(store_id, "SELECT count(*) FROM history.agent_runs") == [(1,)]


def test_an_export_reads_a_batch_of_runs_on_one_connection(
    api, user, store_id, history_tasks, monkeypatch
):
    agent = make_agent(api, "Bot")
    runs = [new_run(api, agent) for _ in range(3)]
    for run in runs:
        chat(api, agent, run, "hi")
    switch(api, True)
    for queued in history_tasks["set_up"]:
        provision.migrate_store(queued)
    connects = []
    connect = history.checkpoints.Connection.connect

    def counted(*args, **kwargs):
        connects.append(1)
        return connect(*args, **kwargs)

    monkeypatch.setattr(history.checkpoints.Connection, "connect", counted)

    assert history.export(store_id) == 3
    assert len(connects) == 1


def test_an_export_under_way_leaves_no_platform_transaction_open(
    api, user, store_id, export_running
):
    # Its lock is the session's, not a transaction's: holding it needn't hold one open.
    with SessionLocal() as db:
        idle = db.scalars(
            text(
                "SELECT query FROM pg_stat_activity"
                " WHERE datname = current_database() AND state = 'idle in transaction'"
                " AND pid <> pg_backend_pid()"
            )
        ).all()
    assert idle == []


# Workflow runs: `history.workflow_runs` and `history.workflow_run_steps`.


@pytest.fixture
def started(monkeypatch) -> list[int]:
    """The ids of the workflow runs the API starts, instead of sending them to the worker."""
    ids = []
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", ids.append)
    return ids


def nested_workflow(api) -> dict:
    """ "pipeline": agent node "draft", then workflow node "review" (agent node "critic")."""
    a = workflow_runs.agents(api, "draft", "critic")
    inner = make_workflow(api, "review", [agent_node("critic", a["critic"])])
    return make_workflow(
        api,
        "pipeline",
        [agent_node("draft", a["draft"]), workflow_node("review", inner)],
        [("draft", "review")],
    )


def test_a_finished_workflow_run_is_exported_with_every_step_linked_to_its_agent_runs(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    history_tasks["export"].clear()

    done = workflow_runs.run(api, started, workflow, "topic")

    assert done["status"] == "succeeded"
    # The run's end wrote its row in the outbox: the export was queued once that committed.
    with SessionLocal() as db:
        row = db.query(HistoryExport).filter_by(kind="workflow_run", source_id=done["id"]).one()
        assert row.exported_at is None
    assert store_id in history_tasks["export"]
    history.export(store_id)

    assert stored(
        store_id,
        "SELECT id, workflow_id, workflow_name, workflow_version, user_id, status, input, output,"
        " error, started_at IS NOT NULL, finished_at IS NOT NULL FROM history.workflow_runs",
    ) == [
        (
            done["id"],
            workflow["id"],
            "pipeline",
            1,
            user.id,
            "succeeded",
            "topic",
            "critic(draft(topic))",
            None,
            True,
            True,
        )
    ]
    steps = {"/".join(s["path"]): s for s in done["steps"]}
    # Every step at every depth, each agent node's joining its exported agent run.
    assert stored(
        store_id,
        "SELECT s.id, s.workflow_run_id, array_to_string(s.path, '/'), s.kind, s.status,"
        " s.input, s.output, s.error, s.agent_run_id, s.finished_at IS NOT NULL, r.agent_name"
        " FROM history.workflow_run_steps s"
        " LEFT JOIN history.agent_runs r ON r.id = s.agent_run_id ORDER BY s.path",
    ) == [
        (steps["draft"]["id"], done["id"], "draft", "agent", "succeeded", "topic",
         "draft(topic)", None, steps["draft"]["agent_run_id"], True, "draft"),
        (steps["review"]["id"], done["id"], "review", "workflow", "succeeded",
         "draft(topic)", "critic(draft(topic))", None, None, True, None),
        (steps["review/critic"]["id"], done["id"], "review/critic", "agent", "succeeded",
         "draft(topic)", "critic(draft(topic))", None, steps["review/critic"]["agent_run_id"],
         True, "critic"),
    ]  # fmt: skip


def test_switching_it_on_backfills_existing_workflow_runs(
    api, user, store_id, history_tasks, started
):
    workflow = nested_workflow(api)
    done = workflow_runs.run(api, started, workflow, "topic")
    failed = workflow_runs.run(api, started, workflow, "again", fail={"critic"})
    assert failed["status"] == "failed"

    switch(api, True)
    set_up_and_export(store_id, history_tasks)

    assert stored(store_id, "SELECT id, status, error FROM history.workflow_runs ORDER BY id") == [
        (done["id"], "succeeded", None),
        (failed["id"], "failed", failed["error"]),
    ]
    assert stored(
        store_id,
        "SELECT workflow_run_id, array_to_string(path, '/'), status"
        " FROM history.workflow_run_steps ORDER BY workflow_run_id, path",
    ) == [
        (done["id"], "draft", "succeeded"),
        (done["id"], "review", "succeeded"),
        (done["id"], "review/critic", "succeeded"),
        (failed["id"], "draft", "succeeded"),
        (failed["id"], "review", "failed"),
        (failed["id"], "review/critic", "failed"),
    ]
    # Each agent node's agent run went in too, and says which workflow run it was part of.
    assert stored(
        store_id,
        "SELECT count(*) FROM history.workflow_run_steps s JOIN history.agent_runs r"
        " ON r.id = s.agent_run_id AND r.workflow_run_id = s.workflow_run_id",
    ) == [(4,)]


def test_a_workflow_run_exported_midway_is_updated_as_it_goes_and_finishes(
    api, user, store_id, history_tasks, started
):
    """What pausing and resuming a run will do (#31): its row written while it's under way,
    then again at its end. Exported midway, the copy has the run and steps as they were then."""
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    run = workflow_runs.start(api, workflow, "topic")
    midway: list[list[tuple]] = []

    # Queued: in the outbox only once it changes, or a backfill puts it there.
    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(0,)]

    def export_now() -> None:
        with SessionLocal() as db:
            history.enqueue_workflow_runs(db, WorkflowRun.id == run["id"])
            db.commit()
        history.export(store_id)
        midway.append(
            stored(store_id, "SELECT status, output FROM history.workflow_runs")
            + stored(
                store_id,
                "SELECT array_to_string(path, '/'), status, output"
                " FROM history.workflow_run_steps ORDER BY path",
            )
        )

    async def script(messages, info):
        name, text = messages[0].content, messages[-1].content
        if name == "critic":
            await asyncio.to_thread(export_now)
        yield f"{name}({text})"

    with use_model(model(script)):
        asyncio.run(execute_run(run["id"]))

    assert midway == [
        [
            ("running", None),
            ("draft", "succeeded", "draft(topic)"),
            ("review", "running", None),
            ("review/critic", "running", None),
        ]
    ]
    history.export(store_id)
    assert stored(store_id, "SELECT status, output FROM history.workflow_runs") == [
        ("succeeded", "critic(draft(topic))")
    ]
    assert stored(
        store_id,
        "SELECT array_to_string(path, '/'), status FROM history.workflow_run_steps ORDER BY path",
    ) == [("draft", "succeeded"), ("review", "succeeded"), ("review/critic", "succeeded")]


def test_exporting_workflow_runs_again_gives_no_duplicates(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    workflow_runs.run(api, started, workflow, "one")
    workflow_runs.run(api, started, workflow, "two")
    history.export(store_id)
    counts = (
        "SELECT (SELECT count(*) FROM history.workflow_runs),"
        " (SELECT count(*) FROM history.workflow_run_steps),"
        " (SELECT count(*) FROM history.agent_runs)"
    )
    assert stored(store_id, counts) == [(2, 6, 4)]

    for _ in range(2):
        with SessionLocal() as db:
            history.enqueue_everything(db, organisations.membership_of(db, user.id).id)
            db.commit()
        assert history.export(store_id) == 6

    assert stored(store_id, counts) == [(2, 6, 4)]


def test_deleting_a_workflow_run_deletes_its_copy(api, user, store_id, history_tasks, started):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    gone = workflow_runs.run(api, started, workflow, "one")
    kept = workflow_runs.run(api, started, workflow, "two")
    history.export(store_id)
    history_tasks["export"].clear()

    assert api.delete(f"/api/workflows/{workflow['id']}/runs/{gone['id']}").status_code == 204

    assert history_tasks["export"] == [store_id]
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.workflow_runs") == [(kept["id"],)]
    assert stored(store_id, "SELECT DISTINCT workflow_run_id FROM history.workflow_run_steps") == [
        (kept["id"],)
    ]
    # Its agent nodes' agent runs went with it.
    assert stored(store_id, "SELECT DISTINCT workflow_run_id FROM history.agent_runs") == [
        (kept["id"],)
    ]
    with SessionLocal() as db:
        assert not db.query(HistoryExport).filter_by(source_id=gone["id"]).count()


def test_a_workflow_run_deleted_while_it_is_off_loses_its_copy_once_it_is_back_on(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    run = workflow_runs.run(api, started, workflow, "one")
    history.export(store_id)
    switch(api, False)

    assert api.delete(f"/api/workflows/{workflow['id']}/runs/{run['id']}").status_code == 204
    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(1,)]

    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(0,)]
    assert stored(store_id, "SELECT count(*) FROM history.workflow_run_steps") == [(0,)]


def test_a_workflow_run_that_cant_be_queued_leaves_no_copy(
    api, user, store_id, history_tasks, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)

    def broker_down(run_id: int) -> None:
        # Committed, so a backfill (or delete exported history) meanwhile exports it.
        with SessionLocal() as db:
            history.enqueue_everything(db, organisations.membership_of(db, user.id).id)
            db.commit()
        history.export(store_id)
        assert stored(store_id, "SELECT id FROM history.workflow_runs") == [(run_id,)]
        raise ConnectionError("broker down")

    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", broker_down)
    response = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "x"})
    assert response.status_code == 503

    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(0,)]


def test_deleting_an_agent_unlinks_its_runs_from_the_exported_steps(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    run = workflow_runs.run(api, started, workflow, "topic")
    history.export(store_id)
    critic = next(s for s in run["steps"] if s["path"] == ["review", "critic"])

    # Once nothing current uses it, it can go, with its runs. The steps that held them stay
    # without them, as on the platform.
    (review,) = [w for w in api.get("/api/workflows").json() if w["name"] == "review"]
    for gone in (f"workflows/{workflow['id']}", f"workflows/{review['id']}"):
        assert api.delete(f"/api/{gone}").status_code == 204
    assert api.delete(f"/api/agents/{critic['agent_id']}").status_code == 204
    history.export(store_id)

    assert stored(
        store_id,
        "SELECT array_to_string(path, '/'), agent_run_id IS NOT NULL"
        " FROM history.workflow_run_steps ORDER BY path",
    ) == [("draft", True), ("review", False), ("review/critic", False)]
    assert stored(store_id, "SELECT agent_name FROM history.agent_runs") == [("draft",)]


def test_a_store_behind_the_history_lines_head_isnt_exported_until_it_is_brought_there(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow_runs.run(api, started, nested_workflow(api), "topic")
    # As if its last migration failed before the `history` line's newest revision.
    with SessionLocal() as db:
        db.execute(
            update(DataStore)
            .where(DataStore.id == store_id)
            .values(history_revision=migrations.history_revisions_before_head()[0])
        )
        db.commit()

    # The export task tries it again (`test_the_export_task_tries_a_busy_or_behind_store_again`),
    # and the provisioning worker's beat brings it to the head meanwhile.
    with pytest.raises(DataStoreBehind):
        history.export(store_id)
    with SessionLocal() as db:
        assert db.query(HistoryExport).filter(HistoryExport.exported_at.is_(None)).count() == 3
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(0,)]

    provision.migrate_store(store_id)
    history.export(store_id)
    assert stored(store_id, "SELECT count(*) FROM history.workflow_runs") == [(1,)]


def test_delete_exported_history_empties_the_workflow_runs_and_while_on_brings_them_back(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    run = workflow_runs.run(api, started, nested_workflow(api), "topic")
    history.export(store_id)
    counts = (
        "SELECT (SELECT count(*) FROM history.workflow_runs),"
        " (SELECT count(*) FROM history.workflow_run_steps)"
    )
    assert stored(store_id, counts) == [(1, 3)]

    assert api.delete("/api/organisation/settings/exported-history").status_code == 204

    assert stored(store_id, counts) == [(0, 0)]
    history.export(store_id)
    assert stored(store_id, "SELECT id FROM history.workflow_runs") == [(run["id"],)]
    assert stored(store_id, counts) == [(1, 3)]


def test_deleting_a_workflow_steps_agent_run_unlinks_it_from_the_exported_step(
    api, user, store_id, history_tasks, started
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    run = workflow_runs.run(api, started, nested_workflow(api), "topic")
    history.export(store_id)
    critic = next(s for s in run["steps"] if s["path"] == ["review", "critic"])

    url = f"/api/agents/{critic['agent_id']}/runs/{critic['agent_run_id']}"
    assert api.delete(url).status_code == 204
    history.export(store_id)

    assert stored(
        store_id,
        "SELECT array_to_string(path, '/'), agent_run_id IS NOT NULL"
        " FROM history.workflow_run_steps ORDER BY path",
    ) == [("draft", True), ("review", False), ("review/critic", False)]


def test_a_run_ended_while_a_node_runs_starts_no_more_steps_than_its_copy_has(
    api, user, store_id, history_tasks, started
):
    """A run failed as stopped (its heartbeat was late, say) while its worker goes on: the copy
    written as it ended is complete, as no node starts once it has."""
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    workflow = nested_workflow(api)
    run = workflow_runs.start(api, workflow, "topic")

    async def script(messages, info):
        name, text = messages[0].content, messages[-1].content
        if name == "draft":
            await asyncio.to_thread(workflows.fail_run, run["id"], "Stopped elsewhere")
        yield f"{name}({text})"

    with use_model(model(script)):
        asyncio.run(execute_run(run["id"]))
    history.export(store_id)

    out = api.get(f"/api/workflows/{workflow['id']}/runs/{run['id']}").json()
    assert (out["status"], out["error"]) == ("failed", "Stopped elsewhere")
    assert [s["path"] for s in out["steps"]] == [["draft"]]
    assert stored(
        store_id, "SELECT array_to_string(path, '/') FROM history.workflow_run_steps"
    ) == [("draft",)]


def test_a_batch_of_workflow_runs_alone_reads_no_checkpoints(
    api, user, store_id, history_tasks, started, monkeypatch
):
    switch(api, True)
    set_up_and_export(store_id, history_tasks)
    run = workflow_runs.run(api, started, nested_workflow(api), "topic")
    history.export(store_id)
    with SessionLocal() as db:
        history.enqueue_workflow_runs(db, WorkflowRun.id == run["id"])
        db.commit()
    connects = []
    connect = history.checkpoints.Connection.connect

    def counted(*args, **kwargs):
        connects.append(1)
        return connect(*args, **kwargs)

    monkeypatch.setattr(history.checkpoints.Connection, "connect", counted)

    assert history.export(store_id) == 1
    assert connects == []
