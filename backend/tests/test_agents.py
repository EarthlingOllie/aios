import asyncio
import json
import threading
import uuid
from dataclasses import dataclass

import psycopg
import pytest
from fake_model import ToolCall, model, replies, use_model
from sqlalchemy import func

from aios import checkpoints, ui_stream
from aios.agent import ChatDeps
from aios.api import agents as agents_api
from aios.api.skills import lock_live
from aios.auth import current_user
from aios.db import SessionLocal, get_db
from aios.main import app
from aios.models import Agent, AgentRun, User
from aios.tools import hybrid_search
from aios.versioning import save_agent_version


def chat_body(text: str) -> dict:
    return {
        "trigger": "submit-message",
        "id": "run",
        "messages": [{"id": "m", "role": "user", "parts": [{"type": "text", "text": text}]}],
    }


def spy_run(monkeypatch) -> dict:
    """Record what the chat endpoint runs the agent with: its trace metadata and context."""
    seen = {}
    load = agents_api.load_chat_context

    def spy_load(*args):
        ctx = load(*args)
        seen["metadata"] = ctx.metadata
        return ctx

    @dataclass
    class SpyDeps(ChatDeps):
        def __post_init__(self):
            seen["deps"] = ChatDeps(
                user_id=self.user_id, data_store_id=self.data_store_id, run_id=self.run_id
            )

    monkeypatch.setattr(agents_api, "load_chat_context", spy_load)
    monkeypatch.setattr(agents_api, "ChatDeps", SpyDeps)
    return seen


def text_deltas(resp) -> str:
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: {")
    ]
    return "".join(e["delta"] for e in events if e["type"] == "text-delta")


def make_skill(api, name: str, content: str = "", description: str = "") -> dict:
    body = {"name": name, "description": description, "content": content}
    resp = api.post("/api/skills", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_agent(api, name: str, base_prompt: str = "", skills: list[dict] = ()) -> dict:
    body = {
        "name": name,
        "base_prompt": base_prompt,
        "skill_version_ids": [s["current_version"]["id"] for s in skills],
    }
    resp = api.post("/api/agents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def agent_body(agent: dict, **changes) -> dict:
    v = agent["current_version"]
    body = {
        "name": agent["name"],
        "description": agent["description"],
        "base_prompt": v["base_prompt"],
        "skill_version_ids": [s["skill_version_id"] for s in v["skills"]],
    }
    return body | changes


def test_skill_versions(api):
    skill = make_skill(api, "Haiku", "5-7-5", "Poems")
    assert skill["current_version"]["version"] == 1
    assert skill["agents"] == []
    assert api.post("/api/skills", json={"name": "Haiku"}).status_code == 409
    assert api.post("/api/skills", json={"name": "  "}).status_code == 422
    url = f"/api/skills/{skill['id']}"

    # Saving unchanged content doesn't create a version.
    resp = api.put(url, json={"name": "Haiku", "description": "Poems", "content": "5-7-5"})
    assert resp.json()["current_version"]["version"] == 1

    # The name is sent to the model, so renaming does create one, which snapshots the name.
    resp = api.put(url, json={"name": "Haiku!", "description": "Poems", "content": "5-7-5"})
    v2 = resp.json()["current_version"]
    assert (resp.json()["name"], v2["version"], v2["name"]) == ("Haiku!", 2, "Haiku!")

    resp = api.put(url, json={"name": "Haiku!", "content": "5/7/5", "note": "slashes"})
    v3 = resp.json()["current_version"]
    assert (v3["version"], v3["content"], v3["note"]) == (3, "5/7/5", "slashes")
    assert [v["version"] for v in api.get(f"{url}/versions").json()] == [3, 2, 1]
    v1 = api.get(f"{url}/versions/1").json()
    assert (v1["name"], v1["content"]) == ("Haiku", "5-7-5")

    # Restoring makes a new version with the old content under the current name.
    restored = api.post(f"{url}/versions/1/restore").json()["current_version"]
    assert (restored["version"], restored["name"], restored["content"]) == (4, "Haiku!", "5-7-5")
    assert restored["note"] == "Restored from v1"

    listed = api.get("/api/skills").json()
    assert [(s["name"], s["version"]) for s in listed] == [("Haiku!", 4)]


def test_agent_versions_pin_skill_versions(api):
    skill = make_skill(api, "Terse", "Answer briefly.")
    agent = make_agent(api, "Helper", "You help.", [skill])
    v1 = agent["current_version"]
    assert v1["version"] == 1
    assert v1["instructions"].startswith("You help.")
    assert '<skill name="Terse">\nAnswer briefly.\n</skill>' in v1["instructions"]
    url = f"/api/agents/{agent['id']}"

    # Renames and unchanged saves keep the version.
    resp = api.put(url, json=agent_body(agent, name="Helper 2", description="d"))
    assert resp.json()["current_version"]["version"] == 1

    # Editing the skill doesn't change the agent: it stays pinned, with an update available.
    api.put(f"/api/skills/{skill['id']}", json={"name": "Terse", "content": "One line only."})
    pinned = api.get(url).json()["current_version"]["skills"][0]
    assert (pinned["version"], pinned["latest_version"]) == (1, 2)
    assert "Answer briefly." in api.get(url).json()["current_version"]["instructions"]

    # Taking the update is an explicit change, and creates agent v2.
    agent = api.get(url).json()
    resp = api.put(
        url,
        json=agent_body(agent, skill_version_ids=[pinned["latest_version_id"]], note="new skill"),
    )
    v2 = resp.json()["current_version"]
    assert (v2["version"], v2["note"]) == (2, "new skill")
    assert "One line only." in v2["instructions"]
    assert [v["version"] for v in api.get(f"{url}/versions").json()] == [2, 1]
    assert api.get(f"{url}/versions/1").json()["instructions"] == v1["instructions"]

    restored = api.post(f"{url}/versions/1/restore").json()["current_version"]
    assert restored["version"] == 3
    assert restored["instructions"] == v1["instructions"]

    listed = api.get("/api/agents").json()
    assert [(a["name"], a["version"], a["skill_count"]) for a in listed] == [("Helper 2", 3, 1)]

    assert api.put(url, json=agent_body(agent, skill_version_ids=[-1])).status_code == 422
    twice = [pinned["skill_version_id"], pinned["latest_version_id"]]
    assert api.put(url, json=agent_body(agent, skill_version_ids=twice)).status_code == 422


def test_skill_rename_is_pinned_like_content(api, monkeypatch):
    skill = make_skill(api, "Haiku", "5-7-5")
    agent = make_agent(api, "Poet", "Write.", [skill])
    url = f"/api/agents/{agent['id']}"
    run = api.post(f"{url}/runs").json()
    api.put(f"/api/skills/{skill['id']}", json={"name": "Poetry", "content": "5-7-5"})

    # The pinned version keeps the old name in both the instructions and the trace metadata,
    # and re-saving the agent unchanged doesn't pick up the rename.
    agent = api.get(url).json()
    pinned = agent["current_version"]["skills"][0]
    assert (pinned["name"], pinned["version"], pinned["latest_version"]) == ("Haiku", 1, 2)
    assert '<skill name="Haiku">' in agent["current_version"]["instructions"]
    assert api.put(url, json=agent_body(agent)).json()["current_version"]["version"] == 1

    seen = spy_run(monkeypatch)
    with use_model(replies("ok")):
        resp = api.post(f"{url}/runs/{run['id']}/chat", json=chat_body("hi"))
    assert resp.status_code == 200
    assert seen["metadata"]["skill_versions"][0]["name"] == "Haiku"

    # Taking the update sends the new name.
    resp = api.put(url, json=agent_body(agent, skill_version_ids=[pinned["latest_version_id"]]))
    v2 = resp.json()["current_version"]
    assert (v2["version"], v2["skills"][0]["name"]) == (2, "Poetry")
    assert '<skill name="Poetry">' in v2["instructions"]
    assert "Haiku" not in v2["instructions"]


def test_skill_delete_blocked_while_in_use_then_soft(api):
    skill = make_skill(api, "Pirate", "Arr.")
    agent = make_agent(api, "Bot", skills=[skill])
    assert api.get(f"/api/skills/{skill['id']}").json()["agents"] == [
        {"agent_id": agent["id"], "agent_name": "Bot", "agent_version": 1, "skill_version": 1}
    ]
    assert api.delete(f"/api/skills/{skill['id']}").status_code == 409

    url = f"/api/agents/{agent['id']}"
    api.put(url, json=agent_body(agent, skill_version_ids=[]))
    assert api.delete(f"/api/skills/{skill['id']}").status_code == 204
    assert api.get(f"/api/skills/{skill['id']}").status_code == 404
    assert api.get("/api/skills").json() == []

    # v1 still shows the deleted skill, but can't be restored; the name is free again.
    old = api.get(f"{url}/versions/1").json()["skills"][0]
    assert (old["name"], old["latest_version"]) == ("Pirate", None)
    assert api.post(f"{url}/versions/1/restore").status_code == 409
    make_skill(api, "Pirate")

    assert api.delete(url).status_code == 204
    assert api.get("/api/agents").json() == []


def test_skill_delete_waits_for_in_flight_attach(api, user):
    skill = make_skill(api, "Racy", "Go fast.")
    agent = make_agent(api, "Bot")
    result = {}

    with SessionLocal() as session:
        # An agent save that has resolved (and locked) the skill but not yet committed.
        skills = agents_api.resolve_skill_versions(
            session, user.id, [skill["current_version"]["id"]]
        )
        deleter = threading.Thread(
            target=lambda: result.update(resp=api.delete(f"/api/skills/{skill['id']}"))
        )
        deleter.start()
        deleter.join(timeout=0.5)
        assert deleter.is_alive(), "delete should block on the skill row lock"

        save_agent_version(
            session,
            session.get(Agent, agent["id"]),
            base_prompt="",
            skills=skills,
            note=None,
            user_id=user.id,
        )
        session.commit()

    deleter.join(timeout=10)
    # The delete saw the attach once it got the lock, so the agent never points at a dead skill.
    assert result["resp"].status_code == 409
    assert api.get(f"/api/skills/{skill['id']}").status_code == 200


def test_agent_delete_is_soft_and_takes_its_runs(api, db):
    skill = make_skill(api, "Pirate", "Arr.")
    agent = make_agent(api, "Bot", skills=[skill])
    url = f"/api/agents/{agent['id']}"
    run = api.post(f"{url}/runs").json()

    assert api.delete(url).status_code == 204
    for gone in (url, f"{url}/versions", f"{url}/runs/{run['id']}"):
        assert api.get(gone).status_code == 404
    # Its versions are still there to read, though, as a deleted workflow's are: older workflow
    # versions, and their past runs, pin them.
    old = api.get(f"{url}/versions/1")
    assert old.status_code == 200
    assert old.json()["version"] == 1
    assert [s["skill_id"] for s in old.json()["skills"]] == [skill["id"]]
    assert api.get(f"{url}/versions/2").status_code == 404
    assert api.put(url, json=agent_body(agent)).status_code == 404
    assert api.post(f"{url}/runs").status_code == 404
    assert api.delete(url).status_code == 404
    assert api.get("/api/agents").json() == []
    # The row and its versions stay (workflow versions may pin them); its runs don't.
    row = db.get(Agent, agent["id"])
    assert row.deleted_at is not None and row.current_version.version == 1
    assert db.get(AgentRun, run["id"]) is None

    # A deleted agent doesn't hold on to the skills its current version used, nor its name.
    assert api.delete(f"/api/skills/{skill['id']}").status_code == 204
    assert make_agent(api, "Bot")["name"] == "Bot"
    resp = api.post("/api/agents", json={"name": "Bot"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "You already have an agent with that name."


def test_starting_a_run_waits_for_an_in_flight_agent_delete(api):
    agent = make_agent(api, "Bot")
    result = {}

    with SessionLocal() as session:
        # A delete that has locked the agent and found it unused, but not committed yet.
        assert lock_live(session, Agent, [agent["id"]])
        session.get(Agent, agent["id"]).deleted_at = func.now()
        starter = threading.Thread(
            target=lambda: result.update(resp=api.post(f"/api/agents/{agent['id']}/runs"))
        )
        starter.start()
        starter.join(timeout=0.5)
        assert starter.is_alive(), "starting a run should block on the agent row lock"
        session.commit()

    starter.join(timeout=10)
    # Not a run left behind on a deleted agent, which its delete would have missed.
    assert result["resp"].status_code == 404


def test_other_users_cannot_see_agents(api, client, db):
    agent = make_agent(api, "Mine")
    other = User(github_id=-2, login="other")
    db.add(other)
    db.commit()
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(f"/api/agents/{agent['id']}").status_code == 404
        assert client.get(f"/api/agents/{agent['id']}/versions").status_code == 404
        assert client.post(f"/api/agents/{agent['id']}/runs").status_code == 404
        assert client.get("/api/agents").json() == []
    finally:
        db.delete(other)
        db.commit()


def test_other_users_cannot_read_a_deleted_agents_versions(api, client, db):
    agent = make_agent(api, "Mine")
    url = f"/api/agents/{agent['id']}"
    assert api.delete(url).status_code == 204
    assert api.get(f"{url}/versions/1").status_code == 200
    other = User(github_id=-2, login="other")
    db.add(other)
    db.commit()
    try:
        app.dependency_overrides[current_user] = lambda: other
        for gone in (url, f"{url}/versions", f"{url}/versions/1"):
            assert client.get(gone).status_code == 404
    finally:
        db.delete(other)
        db.commit()


def test_ids_beyond_the_integer_columns_are_invalid_not_server_errors(api):
    huge = 2**31  # one past Postgres INTEGER
    agent = make_agent(api, "Bounded")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    skill = make_skill(api, "Bounded skill")
    for url in [
        f"/api/agents/{huge}",
        f"/api/agents/{huge}/versions",
        f"/api/agents/{agent['id']}/versions/{huge}",
        f"/api/agents/{agent['id']}/runs/{huge}",
        f"/api/agents/{agent['id']}/runs/{-huge - 1}",
        f"/api/skills/{huge}",
        f"/api/skills/{skill['id']}/versions/{huge}",
    ]:
        assert api.get(url).status_code == 422, url
    assert api.delete(f"/api/documents/{huge}").status_code == 422
    assert api.post(f"/api/agents/{agent['id']}/runs/{huge}/chat", json={}).status_code == 422
    body = agent_body(agent, skill_version_ids=[huge])
    assert api.post("/api/agents", json={**body, "name": "Huge"}).status_code == 422
    assert api.put(f"/api/agents/{agent['id']}", json=body).status_code == 422
    # The largest INTEGER is valid; it just names nothing.
    assert api.get(f"/api/agents/{huge - 1}").status_code == 404
    assert api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code == 200


def test_run_is_pinned_to_its_version_and_persists_history(api, monkeypatch):
    skill = make_skill(api, "Pirate", "Talk like pirates.")
    agent = make_agent(api, "Bot", "Be nice.", [skill])
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    assert (run["message_count"], run["agent_version"]) == (0, 1)
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    # Editing the agent mid-run doesn't affect the run.
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="Be rude."))

    seen = spy_run(monkeypatch)
    sent = []

    async def script(messages, info):
        sent.append(messages)
        yield "arr"

    with use_model(model(script)):
        first = api.post(f"{url}/chat", json=chat_body("hello there"))
        second = api.post(f"{url}/chat", json=chat_body("again"))
    assert first.status_code == second.status_code == 200
    assert text_deltas(second) == "arr"
    assert seen["metadata"]["agent_version"] == 1
    assert seen["metadata"]["skill_versions"][0]["name"] == "Pirate"
    assert seen["metadata"]["langfuse_session_id"] == f"agent-run-{run['id']}"

    detail = api.get(url).json()
    assert detail["title"] == "hello there"
    assert (detail["message_count"], detail["agent_version"]) == (4, 1)
    # The second turn was sent only its own message; history came from the run's thread.
    assert [m["role"] for m in detail["ui_messages"]] == ["user", "assistant", "user", "assistant"]
    assert [m["type"] for m in detail["messages"]] == ["human", "ai", "human", "ai"]
    assert [(m.type, m.content) for m in sent[1]][1:] == [
        ("human", "hello there"),
        ("ai", "arr"),
        ("human", "again"),
    ]
    # The model is given the pinned version's instructions.
    instructions = sent[1][0].content
    assert instructions.startswith("Be nice.")
    assert "Talk like pirates." in instructions

    # A new run uses the new version.
    assert api.post(f"/api/agents/{agent['id']}/runs").json()["agent_version"] == 2
    versions = api.get(f"/api/agents/{agent['id']}/versions").json()
    assert [(v["version"], v["run_count"]) for v in versions] == [(2, 1), (1, 1)]

    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404


def test_blank_message_is_rejected_before_it_reaches_the_thread(api):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    assert api.post(f"{url}/chat", json=chat_body("  ")).status_code == 422
    with use_model(replies("hi")):
        assert text_deltas(api.post(f"{url}/chat", json=chat_body("hello"))) == "hi"
    assert [m["type"] for m in api.get(url).json()["messages"]] == ["human", "ai"]


async def consume(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def test_concurrent_turns_on_a_run_take_turns(api, user):
    """Two tabs: the second turn waits for the first, and builds on all of it."""
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"
    sent = {}

    async def script(messages, info):
        prompt = messages[-1].content
        sent[prompt] = [m.content for m in messages[1:]]
        if prompt == "from this tab":
            await asyncio.sleep(0.5)
        yield f"re: {prompt}"

    async def two_tabs():
        async def turn(text, delay):
            await asyncio.sleep(delay)
            with SessionLocal() as db:
                response = await agents_api.run_chat(
                    agent["id"], run["id"], chat_body(text), user, db
                )
            return await consume(response)

        return await asyncio.gather(turn("from this tab", 0), turn("from the other tab", 0.1))

    with use_model(model(script)):
        asyncio.run(two_tabs())

    assert sent["from the other tab"] == [
        "from this tab",
        "re: from this tab",
        "from the other tab",
    ]
    assert stored_turns(api, url) == [
        ("user", "from this tab"),
        ("assistant", "re: from this tab"),
        ("user", "from the other tab"),
        ("assistant", "re: from the other tab"),
    ]
    assert api.get(url).json()["message_count"] == 4


def test_deleting_a_run_mid_turn_deletes_all_its_checkpoints(api, user):
    """The deletion waits for the turn, which would otherwise write the thread back."""
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"
    started = asyncio.Event()

    async def script(messages, info):
        started.set()
        await asyncio.sleep(0.5)
        yield "still going"

    async def delete_mid_turn():
        with SessionLocal() as db:
            response = await agents_api.run_chat(
                agent["id"], run["id"], chat_body("hello"), user, db
            )
        turn = asyncio.create_task(consume(response))
        await started.wait()
        resp = await asyncio.to_thread(api.delete, url)
        await turn
        return resp

    with use_model(model(script)):
        assert asyncio.run(delete_mid_turn()).status_code == 204

    assert checkpoints.history(run["id"]) == []


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_chat_releases_request_session_before_streaming(client, db, monkeypatch):
    """DB work runs off the event loop, and the request's session is closed before the stream."""
    events = []
    checkpoints.delete_threads([1])

    def fake_db():
        events.append("db opened")
        yield object()
        events.append("db closed")

    def fake_load(db, user_id, agent_id, run_id):
        events.append(("load on loop", on_event_loop()))
        return agents_api.ChatContext(
            model=None, instructions="Hi.", metadata={}, data_store_id=uuid.uuid4()
        )

    def fake_start(run_id, prompt):
        events.append(("title on loop", on_event_loop()))

    def fake_end(run_id, count):
        events.append(("count on loop", on_event_loop(), count))

    async def script(messages, info):
        events.append("model")
        yield "arr"

    async def run_there(conn, run_id):
        return False

    monkeypatch.setattr(agents_api, "load_chat_context", fake_load)
    monkeypatch.setattr(agents_api, "_start_turn", fake_start)
    monkeypatch.setattr(agents_api, "_end_turn", fake_end)
    # Run 1 is made up, like the rest of what the turn loads.
    monkeypatch.setattr(checkpoints, "_run_gone", run_there)
    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")
    app.dependency_overrides[get_db] = fake_db
    try:
        with use_model(model(script)):
            resp = client.post("/api/agents/1/runs/1/chat", json=chat_body("hello"))
    finally:
        app.dependency_overrides.clear()
        checkpoints.delete_threads([1])

    assert resp.status_code == 200
    assert text_deltas(resp) == "arr"
    assert events == [
        "db opened",
        ("load on loop", False),
        ("title on loop", False),
        "db closed",
        "model",
        ("count on loop", False, 2),
    ]


def stored_turns(api, url) -> list[tuple[str, str]]:
    detail = api.get(url).json()
    return [
        (m["role"], p["text"])
        for m in detail["ui_messages"]
        for p in m["parts"]
        if p["type"] == "text"
    ]


def test_failed_turn_keeps_the_users_message_and_partial_reply(api):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    async def fails_midway(messages, info):
        yield "half an "
        raise RuntimeError("model fell over")

    with use_model(model(fails_midway)):
        resp = api.post(f"{url}/chat", json=chat_body("hello"))
    assert resp.status_code == 200
    assert '"type":"error"' in resp.text

    assert stored_turns(api, url) == [("user", "hello"), ("assistant", "half an ")]
    assert api.get(url).json()["title"] == "hello"

    # The next turn's model call sees the failed exchange.
    seen = []

    async def records_history(messages, info):
        seen.extend(messages)
        yield "ok"

    with use_model(model(records_history)):
        assert api.post(f"{url}/chat", json=chat_body("again")).status_code == 200
    assert [(m.type, m.content) for m in seen[1:]] == [
        ("human", "hello"),
        ("ai", "half an "),
        ("human", "again"),
    ]
    assert stored_turns(api, url)[-2:] == [("user", "again"), ("assistant", "ok")]
    assert api.get(url).json()["message_count"] == 4


def test_database_error_text_isnt_sent_to_the_client(api, monkeypatch):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def connect(*args, **kwargs):
        raise psycopg.OperationalError('connection to server at "10.0.0.5" failed for user "aios"')

    monkeypatch.setattr(checkpoints.AsyncConnection, "connect", connect)
    resp = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hello"))
    assert '"errorText":"Database error"' in resp.text
    assert "10.0.0.5" not in resp.text


def test_stopped_turn_keeps_the_users_message_and_partial_reply(api, user, db):
    """Stop in the UI aborts the fetch; the server sees it as the response task being cancelled."""
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    async def slow(messages, info):
        yield "partial "
        await asyncio.sleep(60)
        yield "never sent"

    async def stop_after_first_text():
        response = await agents_api.run_chat(agent["id"], run["id"], chat_body("stop me"), user, db)
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

    assert stored_turns(api, url) == [("user", "stop me"), ("assistant", "partial ")]


def stop_during_searches(
    api, user, db, monkeypatch, events: list, queries=("slow", "quick")
) -> str:
    """Run a turn whose model makes a search for each of `queries` at once, and drop its
    response once a result is streamed (a "slow" search is still running). Returns the run's
    URL."""
    agent = make_agent(api, "Bot")
    tool = {"key": "hybrid_search", "config": {}}
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool]))
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def search(data_store_id, query, limit):
        if query == "slow":
            events.append("search started")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                events.append("search stopped")
                raise
        return [{"source": f"{query}.md", "headings": [], "text": query}]

    async def script(messages, info):
        for query in queries:
            yield ToolCall("search_documents", {"query": query}, id=f"call-{query}")

    monkeypatch.setattr(hybrid_search, "document_search_results", search)
    # The chunks themselves, rather than a response streaming them.
    monkeypatch.setattr(ui_stream, "streaming_response", lambda chunks: chunks)

    async def drop_while_searching():
        chunks = await agents_api.run_chat(agent["id"], run["id"], chat_body("find"), user, db)
        async for chunk in chunks:
            if chunk["type"] == "tool-output-available":
                break
        await chunks.aclose()

    with use_model(model(script)):
        asyncio.run(drop_while_searching())
    return f"/api/agents/{agent['id']}/runs/{run['id']}"


def test_dropped_stream_stops_the_agent_before_the_turn_is_recorded(api, user, db, monkeypatch):
    """The server can drop the response while it's waiting on the client rather than the agent,
    which closes the stream rather than cancelling it. The agent's tool calls must still stop
    before the turn is recorded, or one could answer a call the record says went unanswered."""
    record_turn = agents_api._record_turn
    events = []

    async def spy_record_turn(*args, **kwargs):
        events.append("turn recorded")
        await record_turn(*args, **kwargs)

    monkeypatch.setattr(agents_api, "_record_turn", spy_record_turn)
    stop_during_searches(api, user, db, monkeypatch, events)
    assert events == ["search started", "search stopped", "turn recorded"]


def test_turn_stopped_during_parallel_tool_calls_keeps_their_results(api, user, db, monkeypatch):
    """The quick search finished in the step the turn stopped in, so its result was only a
    pending write: it's kept along with the slow one's, and the next turn is sent both."""
    url = stop_during_searches(api, user, db, monkeypatch, [])

    messages = api.get(url).json()["messages"]
    assert [(m["type"], m.get("tool_call_id"), m.get("status")) for m in messages] == [
        ("human", None, None),
        ("ai", None, None),
        ("tool", "call-quick", "success"),
        ("tool", "call-slow", "error"),
    ]

    seen = []

    async def records_history(messages, info):
        seen.extend(messages)
        yield "ok"

    monkeypatch.undo()  # a response again
    with use_model(model(records_history)):
        assert api.post(f"{url}/chat", json=chat_body("again")).status_code == 200
    assert [(m.type, getattr(m, "tool_call_id", None)) for m in seen[1:]] == [
        ("human", None),
        ("ai", None),
        ("tool", "call-quick"),
        ("tool", "call-slow"),
        ("human", None),
    ]


def test_turn_stopped_once_its_tool_calls_finished_keeps_their_results(api, user, db, monkeypatch):
    """Both searches finished, but the turn stopped before their step was checkpointed."""
    url = stop_during_searches(api, user, db, monkeypatch, [], queries=("one", "two"))

    messages = api.get(url).json()["messages"]
    assert [(m["type"], m.get("tool_call_id"), m.get("status")) for m in messages] == [
        ("human", None, None),
        ("ai", None, None),
        ("tool", "call-one", "success"),
        ("tool", "call-two", "success"),
    ]
