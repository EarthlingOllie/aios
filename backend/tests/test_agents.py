import json

import pytest
from pydantic_ai.models.test import TestModel

from aios.api import agents as agents_api
from aios.auth import current_user
from aios.main import app
from aios.models import User
from aios.runner import runner


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
    assert (pinned["version"], pinned["latest_version"]) == (1, 2)
    assert '<skill name="Haiku">' in agent["current_version"]["instructions"]
    assert api.put(url, json=agent_body(agent)).json()["current_version"]["version"] == 1

    seen = {}
    dispatch = agents_api.VercelAIAdapter.dispatch_request

    async def spy(request, **kwargs):
        seen.update(kwargs)
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(agents_api.VercelAIAdapter, "dispatch_request", spy)
    with runner.override(model=TestModel(custom_output_text="ok")):
        resp = api.post(f"{url}/runs/{run['id']}/chat", json=chat_body("hi"))
    assert resp.status_code == 200
    assert seen["metadata"]["skill_versions"][0]["name"] == "Haiku"

    # Taking the update sends the new name.
    resp = api.put(url, json=agent_body(agent, skill_version_ids=[pinned["latest_version_id"]]))
    v2 = resp.json()["current_version"]
    assert v2["version"] == 2
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

    seen = {}
    dispatch = agents_api.VercelAIAdapter.dispatch_request

    async def spy(request, **kwargs):
        seen.update(kwargs)
        return await dispatch(request, **kwargs)

    monkeypatch.setattr(agents_api.VercelAIAdapter, "dispatch_request", spy)
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
