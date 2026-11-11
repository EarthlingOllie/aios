"""Runs are in each organisation's store: agent runs, their sandbox sessions, workflow runs and
their steps, and each run's history, kept by the store's own checkpointer. The platform database
holds none of them."""

from conftest import delete_user, provisioned_user, store_of, user_store
from sqlalchemy import text
from test_agents import chat_body, make_agent

from aios import checkpoints
from aios.auth import current_user
from aios.main import app
from aios.testing.fake_model import model, replies, use_model

RUN_TABLES = ("agent_runs", "sandbox_sessions", "workflow_runs", "workflow_run_steps")
CHECKPOINTER_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def test_the_platform_holds_no_runs_and_no_checkpoints_and_each_store_does(db, user):
    for table in (*RUN_TABLES, *CHECKPOINTER_TABLES, "checkpoint_migrations"):
        assert db.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None, table
    assert db.scalar(text("SELECT to_regnamespace('langgraph')")) is None
    with user_store(user) as store:
        for table in RUN_TABLES:
            assert store.scalar(text("SELECT to_regclass(:t)"), {"t": f"aios.{table}"}), table
        for table in CHECKPOINTER_TABLES:
            assert store.scalar(text("SELECT to_regclass(:t)"), {"t": f"langgraph.{table}"})


def threads(user, run_id: int) -> int:
    """Rows of the run's thread in the user's store's checkpointer."""
    with user_store(user) as store:
        return store.scalar(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t"),
            {"t": checkpoints.thread_id(run_id)},
        )


def test_a_new_turn_continues_the_runs_history_from_the_stores_checkpointer(api, user):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"
    seen = []

    async def script(messages, info):
        seen.append([m.content for m in messages[1:]])
        yield f"reply {len(seen)}"

    with use_model(model(script)):
        assert api.post(f"{url}/chat", json=chat_body("first")).status_code == 200
        assert threads(user, run["id"]) > 0
        assert api.post(f"{url}/chat", json=chat_body("second")).status_code == 200

    # The second turn started from what the first left, read from the store.
    assert seen[1] == ["first", "reply 1", "second"]
    history = checkpoints.history(store_of(user), run["id"])
    assert [m.text for m in history] == ["first", "reply 1", "second", "reply 2"]
    assert api.get(url).json()["message_count"] == 4


def test_deleting_a_run_removes_its_history_from_the_store(api, user):
    agent = make_agent(api, "Bot")
    run, kept = (api.post(f"/api/agents/{agent['id']}/runs").json() for _ in range(2))
    with use_model(replies("hi")):
        for r in (run, kept):
            url = f"/api/agents/{agent['id']}/runs/{r['id']}/chat"
            assert api.post(url, json=chat_body("hello")).status_code == 200
    assert threads(user, run["id"]) > 0

    assert api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code == 204

    with user_store(user) as store:
        for table in CHECKPOINTER_TABLES:
            left = store.scalar(
                text(f"SELECT count(*) FROM langgraph.{table} WHERE thread_id = :t"),
                {"t": checkpoints.thread_id(run["id"])},
            )
            assert left == 0, table
    assert checkpoints.history(store_of(user), run["id"]) == []
    # Only its own: another run's history stays.
    assert threads(user, kept["id"]) > 0


def test_run_ids_repeat_across_organisations_and_each_keeps_its_own_history(api, client, db, user):
    agent = make_agent(api, "Bot")
    mine = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(replies("mine")):
        api.post(f"/api/agents/{agent['id']}/runs/{mine['id']}/chat", json=chat_body("me"))

    other = provisioned_user(db, "other-runs")
    try:
        app.dependency_overrides[current_user] = lambda: other
        their_agent = make_agent(client, "Bot")
        theirs = client.post(f"/api/agents/{their_agent['id']}/runs").json()
        # The same ids, each in its own store.
        assert (their_agent["id"], theirs["id"]) == (agent["id"], mine["id"])
        with use_model(replies("theirs")):
            url = f"/api/agents/{their_agent['id']}/runs/{theirs['id']}/chat"
            assert client.post(url, json=chat_body("them")).status_code == 200
        shown = client.get(f"/api/agents/{their_agent['id']}/runs/{theirs['id']}").json()
        assert [m["content"] for m in shown["messages"]] == ["them", "theirs"]
        their_history = checkpoints.history(store_of(other), theirs["id"])
    finally:
        app.dependency_overrides[current_user] = lambda: user
        delete_user(db, other)

    assert [m.text for m in their_history] == ["them", "theirs"]
    assert [m.text for m in checkpoints.history(store_of(user), mine["id"])] == ["me", "mine"]
