import asyncio
import json
import threading
from dataclasses import dataclass

import psycopg
from conftest import (
    delete_user,
    provisioned_user,
    run_turn,
    store_of,
    take_turn,
    user_store,
)
from sqlalchemy import func

from aios import checkpoints
from aios.agent import ChatDeps
from aios.api import agents as agents_api
from aios.api.skills import lock_live
from aios.auth import current_user
from aios.datastores.models import Agent
from aios.main import app
from aios.testing.fake_model import model, replies, use_model
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

    def spy_load(*args, **kwargs):
        ctx = load(*args, **kwargs)
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


def test_agent_list_names_the_current_versions_skills_and_tools(api):
    terse = make_skill(api, "Terse", "Answer briefly.")
    haiku = make_skill(api, "Haiku", "5-7-5")
    poet = make_agent(api, "Poet", "Write.", [terse, haiku])
    url = f"/api/agents/{poet['id']}"
    web = {"key": "web_search", "config": {}}
    search = {"key": "hybrid_search", "config": {}}
    api.put(url, json=agent_body(poet, tools=[web, search]))
    make_agent(api, "Plain")

    # Updating a skill leaves the agent on its old version of it: still the same skill.
    api.put(f"/api/skills/{terse['id']}", json={"name": "Terse", "content": "One line."})
    listed = {a["name"]: (a["skill_ids"], a["tool_keys"]) for a in api.get("/api/agents").json()}
    assert listed == {
        "Poet": ([terse["id"], haiku["id"]], ["web_search", "hybrid_search"]),
        "Plain": ([], []),
    }

    # Only the current version counts: what an older version had is gone from the list.
    poet = api.get(url).json()
    api.put(url, json=agent_body(poet, skill_version_ids=[haiku["current_version"]["id"]]))
    listed = {a["name"]: (a["skill_ids"], a["tool_keys"]) for a in api.get("/api/agents").json()}
    assert listed["Poet"] == ([haiku["id"]], [])


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
        take_turn(api, agent["id"], run["id"], "hi")
    assert seen["metadata"]["skill_versions"][0]["name"] == "Haiku"

    # Taking the update sends the new name.
    resp = api.put(url, json=agent_body(agent, skill_version_ids=[pinned["latest_version_id"]]))
    v2 = resp.json()["current_version"]
    assert (v2["version"], v2["skills"][0]["name"]) == (2, "Poetry")
    assert '<skill name="Poetry">' in v2["instructions"]
    assert "Haiku" not in v2["instructions"]


def test_skill_list_counts_the_live_agents_using_each_skill(api):
    pirate = make_skill(api, "Pirate", "Arr.")
    terse = make_skill(api, "Terse", "One line.")
    make_skill(api, "Unused")
    make_agent(api, "Both", skills=[pirate, terse])
    make_agent(api, "Pirate only", skills=[pirate])
    dropped = make_agent(api, "Dropped", skills=[terse])
    gone = make_agent(api, "Gone", skills=[terse])
    # An agent still pinning an older version of the skill counts as using it.
    api.put(f"/api/skills/{pirate['id']}", json={"name": "Pirate", "content": "Arr!"})

    def counts() -> dict[str, int]:
        return {s["name"]: s["agent_count"] for s in api.get("/api/skills").json()}

    assert counts() == {"Pirate": 2, "Terse": 3, "Unused": 0}

    # Only an agent's current version counts, not one it has moved on from, and only live agents.
    url = f"/api/agents/{dropped['id']}"
    assert api.put(url, json=agent_body(dropped, skill_version_ids=[])).status_code == 200
    assert api.post(f"/api/agents/{gone['id']}/archive").status_code == 200
    assert counts() == {"Pirate": 2, "Terse": 1, "Unused": 0}


def test_an_archived_skill_keeps_its_versions_and_its_name(api):
    skill = make_skill(api, "Pirate", "Arr.")
    agent = make_agent(api, "Bot", skills=[skill])
    assert api.get(f"/api/skills/{skill['id']}").json()["agents"] == [
        {"agent_id": agent["id"], "agent_name": "Bot", "agent_version": 1, "skill_version": 1}
    ]
    url = f"/api/agents/{agent['id']}"
    api.put(url, json=agent_body(agent, skill_version_ids=[]))
    assert api.post(f"/api/skills/{skill['id']}/archive").status_code == 200
    assert api.get("/api/skills").json() == []

    # v1 still shows the archived skill, but can't be restored; the name stays reserved.
    old = api.get(f"{url}/versions/1").json()["skills"][0]
    assert (old["name"], old["latest_version"], old["archived"]) == ("Pirate", None, True)
    assert api.post(f"{url}/versions/1/restore").status_code == 409
    assert api.post("/api/skills", json={"name": "Pirate"}).status_code == 409


def test_skill_archive_waits_for_in_flight_attach(api, user):
    skill = make_skill(api, "Racy", "Go fast.")
    agent = make_agent(api, "Bot")
    result = {}

    with user_store(user) as session:
        # An agent save that has resolved (and locked) the skill but not yet committed.
        skills = agents_api.resolve_skill_versions(session, [skill["current_version"]["id"]])
        archiver = threading.Thread(
            target=lambda: result.update(resp=api.post(f"/api/skills/{skill['id']}/archive"))
        )
        archiver.start()
        archiver.join(timeout=0.5)
        assert archiver.is_alive(), "archive should block on the skill row lock"

        save_agent_version(
            session,
            session.get(Agent, agent["id"]),
            base_prompt="",
            skills=skills,
            note=None,
            user_id=user.id,
        )
        session.commit()

    archiver.join(timeout=10)
    # Never refused: the attach went first, so the archive lists the agent as using it.
    assert result["resp"].status_code == 200
    used = api.get(f"/api/skills/{skill['id']}/used-by").json()["agents"]
    assert [a["agent_name"] for a in used] == ["Bot"]


def test_starting_a_run_waits_for_an_in_flight_agent_archive(api, user):
    agent = make_agent(api, "Bot")
    result = {}

    with user_store(user) as session:
        # An archive that has locked the agent, but not committed yet.
        assert lock_live(session, Agent, [agent["id"]])
        session.get(Agent, agent["id"]).archived_at = func.now()
        starter = threading.Thread(
            target=lambda: result.update(resp=api.post(f"/api/agents/{agent['id']}/runs"))
        )
        starter.start()
        starter.join(timeout=0.5)
        assert starter.is_alive(), "starting a run should block on the agent row lock"
        session.commit()

    starter.join(timeout=10)
    # Not a run started on an archived agent.
    assert result["resp"].status_code == 409


def test_other_users_cannot_see_agents(api, client, db):
    agent = make_agent(api, "Mine")
    # Another organisation, with a store of its own, which has no agents: ids are each store's
    # own, so the same id names nothing there.
    other = provisioned_user(db, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(f"/api/agents/{agent['id']}").status_code == 404
        assert client.get(f"/api/agents/{agent['id']}/versions").status_code == 404
        assert client.post(f"/api/agents/{agent['id']}/runs").status_code == 404
        assert client.get("/api/agents").json() == []
    finally:
        delete_user(db, other)


def test_other_users_cannot_read_an_archived_agents_versions(api, client, db):
    agent = make_agent(api, "Mine")
    url = f"/api/agents/{agent['id']}"
    assert api.post(f"{url}/archive").status_code == 200
    assert api.get(f"{url}/versions/1").status_code == 200
    other = provisioned_user(db, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        for gone in (url, f"{url}/versions", f"{url}/versions/1"):
            assert client.get(gone).status_code == 404
    finally:
        delete_user(db, other)


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
        take_turn(api, agent["id"], run["id"], "hello there")
        second = take_turn(api, agent["id"], run["id"], "again")
    assert second.text_deltas() == "arr"
    assert seen["metadata"]["agent_version"] == 1
    assert seen["metadata"]["skill_versions"][0]["name"] == "Pirate"
    # Named by its organisation too: run ids are each store's own.
    org_id = seen["metadata"]["organisation_id"]
    assert seen["metadata"]["langfuse_session_id"] == f"agent-run-{org_id}-{run['id']}"

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
            return await run_turn(store_of(user), agent["id"], run["id"], text)

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
        turn = asyncio.create_task(run_turn(store_of(user), agent["id"], run["id"], "hello"))
        await started.wait()
        resp = await asyncio.to_thread(api.delete, url)
        await turn
        return resp

    with use_model(model(script)):
        assert asyncio.run(delete_mid_turn()).status_code == 204

    assert checkpoints.history(store_of(user), run["id"]) == []


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
        resp = take_turn(api, agent["id"], run["id"], "hello")
    assert '"type":"error"' in resp.text

    assert stored_turns(api, url) == [("user", "hello"), ("assistant", "half an ")]
    assert api.get(url).json()["title"] == "hello"

    # The next turn's model call sees the failed exchange.
    seen = []

    async def records_history(messages, info):
        seen.extend(messages)
        yield "ok"

    with use_model(model(records_history)):
        take_turn(api, agent["id"], run["id"], "again")
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
    resp = take_turn(api, agent["id"], run["id"], "hello")
    assert '"errorText":"Database error"' in resp.text
    assert "10.0.0.5" not in resp.text


def test_run_pages_are_read_only_and_the_web_server_runs_no_turns(api):
    """People talk to agents by mentioning them in chats, and the run service runs every turn:
    a run's page reads it, and nothing posts to it."""
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    assert api.post(f"{url}/chat", json={"messages": []}).status_code in (404, 405)
    paths = app.openapi()["paths"]
    run_routes = {
        method
        for path, methods in paths.items()
        if "/agents/{agent_id}/runs/{run_id}" in path
        for method in methods
    }
    assert run_routes == {"get", "delete"}
    assert api.get(url).status_code == 200
