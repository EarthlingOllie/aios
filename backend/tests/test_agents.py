import json

import pytest
from pydantic_ai.models.test import TestModel

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


def test_skill_crud(api):
    body = {"name": "Haiku", "description": "Poems", "content": "5-7-5"}
    resp = api.post("/api/skills", json=body)
    assert resp.status_code == 201
    skill = resp.json()
    assert skill["agents"] == []

    assert api.post("/api/skills", json={"name": "Haiku"}).status_code == 409
    assert api.post("/api/skills", json={"name": "  "}).status_code == 422
    assert [s["name"] for s in api.get("/api/skills").json()] == ["Haiku"]

    resp = api.put(f"/api/skills/{skill['id']}", json={"name": "Haiku", "content": "5/7/5"})
    assert resp.json()["content"] == "5/7/5"

    assert api.delete(f"/api/skills/{skill['id']}").status_code == 204
    assert api.get(f"/api/skills/{skill['id']}").status_code == 404


def test_agent_crud_and_skills(api):
    skill = api.post("/api/skills", json={"name": "Terse", "content": "Answer briefly."}).json()
    resp = api.post(
        "/api/agents",
        json={"name": "Helper", "base_prompt": "You help.", "skill_ids": [skill["id"]]},
    )
    assert resp.status_code == 201
    agent = resp.json()
    assert [s["name"] for s in agent["skills"]] == ["Terse"]
    assert agent["instructions"].startswith("You help.")
    assert '<skill name="Terse">\nAnswer briefly.\n</skill>' in agent["instructions"]
    assert api.get(f"/api/skills/{skill['id']}").json()["agents"] == [
        {"id": agent["id"], "name": "Helper"}
    ]

    listed = api.get("/api/agents").json()
    assert [(a["name"], a["skill_count"], a["run_count"]) for a in listed] == [("Helper", 1, 0)]

    bad = api.put(f"/api/agents/{agent['id']}", json={"name": "Helper", "skill_ids": [-1]})
    assert bad.status_code == 422

    # Deleting a skill detaches it from the agent.
    api.delete(f"/api/skills/{skill['id']}")
    assert api.get(f"/api/agents/{agent['id']}").json()["skills"] == []

    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204
    assert api.get("/api/agents").json() == []


def test_other_users_cannot_see_agents(api, client, db):
    agent = api.post("/api/agents", json={"name": "Mine"}).json()
    other = User(github_id=-2, login="other")
    db.add(other)
    db.commit()
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(f"/api/agents/{agent['id']}").status_code == 404
        assert client.post(f"/api/agents/{agent['id']}/runs").status_code == 404
        assert client.get("/api/agents").json() == []
    finally:
        db.delete(other)
        db.commit()


def test_run_persists_history_across_turns(api):
    skill = api.post("/api/skills", json={"name": "Pirate", "content": "Talk like pirates."}).json()
    agent = api.post(
        "/api/agents", json={"name": "Bot", "base_prompt": "Be nice.", "skill_ids": [skill["id"]]}
    ).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    assert run["message_count"] == 0
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    model = TestModel(custom_output_text="arr")
    with runner.override(model=model):
        first = api.post(f"{url}/chat", json=chat_body("hello there"))
        second = api.post(f"{url}/chat", json=chat_body("again"))
    assert first.status_code == second.status_code == 200
    assert text_deltas(second) == "arr"

    detail = api.get(url).json()
    assert detail["title"] == "hello there"
    assert detail["message_count"] == 4
    # The second turn was sent only its own message; history came from the database.
    assert [m["role"] for m in detail["ui_messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["messages"][-2]["instructions"].startswith("Be nice.")
    assert "Talk like pirates." in detail["messages"][-2]["instructions"]

    runs = api.get(f"/api/agents/{agent['id']}/runs").json()
    assert [(r["id"], r["message_count"]) for r in runs] == [(run["id"], 4)]

    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404
