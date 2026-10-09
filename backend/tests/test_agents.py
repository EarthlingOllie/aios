import asyncio
import json
import threading

import pytest
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from starlette.requests import Request

from aios.api import agents as agents_api
from aios.auth import current_user
from aios.db import SessionLocal, get_db
from aios.main import app
from aios.models import Agent, User
from aios.runner import runner
from aios.versioning import save_agent_version


@pytest.fixture
def api(client, user):
    app.dependency_overrides[current_user] = lambda: user
    yield client
    app.dependency_overrides.clear()


def chat_body(text: str) -> dict:
    return {
        "trigger": "submit-message",
        "id": "run",
        "messages": [{"id": "m", "role": "user", "parts": [{"type": "text", "text": text}]}],
    }


def spy_run_stream(monkeypatch) -> dict:
    """Record the keyword arguments the chat endpoint runs the agent with."""
    seen = {}
    run_stream = agents_api.VercelAIAdapter.run_stream

    def spy(self, **kwargs):
        seen.update(kwargs)
        return run_stream(self, **kwargs)

    monkeypatch.setattr(agents_api.VercelAIAdapter, "run_stream", spy)
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

    seen = spy_run_stream(monkeypatch)
    with runner.override(model=TestModel(custom_output_text="ok")):
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


def test_run_is_pinned_to_its_version_and_persists_history(api, monkeypatch):
    skill = make_skill(api, "Pirate", "Talk like pirates.")
    agent = make_agent(api, "Bot", "Be nice.", [skill])
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    assert (run["message_count"], run["agent_version"]) == (0, 1)
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    # Editing the agent mid-run doesn't affect the run.
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="Be rude."))

    seen = spy_run_stream(monkeypatch)
    with runner.override(model=TestModel(custom_output_text="arr")):
        first = api.post(f"{url}/chat", json=chat_body("hello there"))
        second = api.post(f"{url}/chat", json=chat_body("again"))
    assert first.status_code == second.status_code == 200
    assert text_deltas(second) == "arr"
    assert seen["metadata"]["agent_version"] == 1
    assert seen["metadata"]["skill_versions"][0]["name"] == "Pirate"

    detail = api.get(url).json()
    assert detail["title"] == "hello there"
    assert (detail["message_count"], detail["agent_version"]) == (4, 1)
    # The second turn was sent only its own message; history came from the database.
    assert [m["role"] for m in detail["ui_messages"]] == ["user", "assistant", "user", "assistant"]
    instructions = detail["messages"][-2]["instructions"]
    assert instructions.startswith("Be nice.")
    assert "Talk like pirates." in instructions

    # A new run uses the new version.
    assert api.post(f"/api/agents/{agent['id']}/runs").json()["agent_version"] == 2
    versions = api.get(f"/api/agents/{agent['id']}/versions").json()
    assert [(v["version"], v["run_count"]) for v in versions] == [(2, 1), (1, 1)]

    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404


def test_reply_messages_leave_out_the_saved_request():
    agent = PydanticAgent(TestModel(custom_output_text="arr"))
    first = agent.run_sync("hello")
    history = [*first.all_messages(), ModelRequest(parts=[UserPromptPart("again")])]
    second = agent.run_sync(message_history=history, run_id="turn")
    reply = agents_api.reply_messages(second.all_messages(), "turn")
    assert [type(m) for m in reply] == [ModelResponse]
    assert reply[0].parts[0].content == "arr"
    assert agents_api.reply_messages(first.all_messages(), "turn") == []


def test_concurrent_turns_on_a_run_are_both_kept(api, monkeypatch):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"
    run_stream = agents_api.VercelAIAdapter.run_stream
    other_turn = [
        ModelRequest(parts=[UserPromptPart("from the other tab")]),
        ModelResponse(parts=[TextPart("ok")]),
    ]

    def other_tab_finishes_first(self, **kwargs):
        # This request has already loaded the (empty) history and saved its own message;
        # another turn lands meanwhile.
        agents_api._append_run_messages(run["id"], other_turn)
        return run_stream(self, **kwargs)

    monkeypatch.setattr(agents_api.VercelAIAdapter, "run_stream", other_tab_finishes_first)
    with runner.override(model=TestModel(custom_output_text="arr")):
        assert api.post(f"{url}/chat", json=chat_body("from this tab")).status_code == 200

    detail = api.get(url).json()
    assert detail["message_count"] == 4
    assert detail["title"] == "from this tab"
    # Nothing is lost: this turn's message was saved before the model was called, and its
    # reply is appended after whatever landed in the meantime.
    texts = [p["text"] for m in detail["ui_messages"] for p in m["parts"] if p["type"] == "text"]
    assert texts == ["from this tab", "from the other tab", "ok", "arr"]


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_chat_releases_request_session_before_streaming(client, monkeypatch):
    """DB work runs off the event loop, and the request's session is closed before the stream."""
    events = []

    def fake_db():
        events.append("db opened")
        yield object()
        events.append("db closed")

    def fake_load(db, user_id, agent_id, run_id):
        events.append(("load on loop", on_event_loop()))
        return agents_api.ChatContext(model=None, instructions="Hi.", history=[], metadata={})

    def fake_save(run_id, messages):
        events.append(("save on loop", on_event_loop()))

    async def stream(messages, info):
        events.append("model")
        yield "arr"

    monkeypatch.setattr(agents_api, "load_chat_context", fake_load)
    monkeypatch.setattr(agents_api, "_append_run_messages", fake_save)
    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")
    app.dependency_overrides[get_db] = fake_db
    try:
        with runner.override(model=FunctionModel(stream_function=stream)):
            resp = client.post("/api/agents/1/runs/1/chat", json=chat_body("hello"))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert text_deltas(resp) == "arr"
    assert events == [
        "db opened",
        ("load on loop", False),
        ("save on loop", False),  # the user's message, before the model is called
        "db closed",
        "model",
        ("save on loop", False),  # the reply
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

    with runner.override(model=FunctionModel(stream_function=fails_midway)):
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

    with runner.override(model=FunctionModel(stream_function=records_history)):
        assert api.post(f"{url}/chat", json=chat_body("again")).status_code == 200
    prompts = [p.content for m in seen for p in m.parts if isinstance(p, UserPromptPart)]
    assert prompts == ["hello", "again"]
    assert any(isinstance(p, TextPart) and p.content == "half an " for m in seen for p in m.parts)
    assert stored_turns(api, url)[-2:] == [("user", "again"), ("assistant", "ok")]


def test_stopped_turn_keeps_the_users_message_and_partial_reply(api, user, db):
    """Stop in the UI aborts the fetch; the server sees it as the response task being cancelled."""
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"
    body = json.dumps(chat_body("stop me")).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": url,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )

    async def slow(messages, info):
        yield "partial "
        await asyncio.sleep(60)
        yield "never sent"

    async def stop_after_first_text():
        response = await agents_api.run_chat(agent["id"], run["id"], request, user, db)
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

    with runner.override(model=FunctionModel(stream_function=slow)):
        asyncio.run(stop_after_first_text())

    assert stored_turns(api, url) == [("user", "stop me"), ("assistant", "partial ")]
