"""Exporting and importing agents and workflows.

`GET /api/agents/{id}/export` and `GET /api/workflows/{id}/export` (an editor's, `?version=` a
version number, else the current one) answer a JSON file holding the version's configuration:
its agents, skills and nested workflows, each once. `POST /api/imports` (an editor's, a file
upload, `?dry_run=true` to preview) makes new, unconnected copies of what a file holds, at
version 1.
"""

import json

import pytest
from conftest import delete_user, provisioned_user, user_store
from test_agents import agent_body, make_agent, make_skill
from test_chats import signed_in_as
from test_permissions import participant  # noqa: F401 (fixture)
from test_workflows import agent_node, make_workflow, workflow_node

from aios import assistant
from aios.config import get_settings
from aios.datastores.models import Agent

WEB = {"key": "web_search", "config": {}}


@pytest.fixture
def other(db, api):
    """A second organisation's admin, with a ready store: where files are taken."""
    u = provisioned_user(db, "other", first_name="Olga", last_name="Other")
    yield u
    delete_user(db, u)


def export(api, kind: str, thing: dict, version: int | None = None) -> dict:
    params = {} if version is None else {"version": version}
    resp = api.get(f"/api/{kind}s/{thing['id']}/export", params=params)
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers["content-disposition"]
    return resp.json()


def upload(api, file: dict | bytes, *, dry_run: bool = False, name: str = "bot.json"):
    data = file if isinstance(file, bytes) else json.dumps(file).encode()
    return api.post(
        "/api/imports",
        params={"dry_run": "true"} if dry_run else {},
        files={"file": (name, data, "application/json")},
    )


def take(api, file: dict, **kwargs) -> dict:
    resp = upload(api, file, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def names(api, kind: str) -> list[str]:
    """The organisation's agents, skills or workflows by name, but the organisation assistant
    every organisation starts with (`aios.assistant`)."""
    return sorted(x["name"] for x in api.get(f"/api/{kind}").json() if x["name"] != assistant.NAME)


def tool_agent(api, name: str, skills=(), tools=(WEB,), **body) -> dict:
    agent = make_agent(api, name, "Be brief.", skills=list(skills))
    resp = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=list(tools), **body))
    assert resp.status_code == 200, resp.text
    return resp.json()


# Exports.


def test_an_agent_export_holds_its_configuration_and_pinned_skills_and_nothing_else(api):
    haiku = make_skill(api, "Haiku", "5-7-5", "Poems")
    agent = tool_agent(api, "Poet", skills=[haiku], description="Writes poems")
    # A newer skill version than the agent pins: the export holds the pinned one.
    api.put(f"/api/skills/{haiku['id']}", json={"name": "Haiku", "content": "changed"})

    file = export(api, "agent", agent)

    assert file["format"] == "aios-export"
    assert file["format_version"] == 1
    assert file["exported_at"]
    ref = file["root"]["ref"]
    assert file["root"] == {"kind": "agent", "ref": ref}
    assert file["workflows"] == {}
    (skill_ref,) = file["skills"]
    assert file["skills"][skill_ref] == {
        "name": "Haiku",
        "description": "Poems",
        "content": "5-7-5",
    }
    assert file["agents"] == {
        ref: {
            "name": "Poet",
            "description": "Writes poems",
            "model": None,
            "base_prompt": "Be brief.",
            "tools": [
                {"key": "web_search", "settings": agent["current_version"]["tools"][0]["config"]}
            ],
            "skills": [skill_ref],
        }
    }
    # No ids, people, runs or markers anywhere in it.
    text = json.dumps(file)
    for word in ("_id", "created_by", "builtin", "archived", "note"):
        assert word not in text


def test_an_older_version_can_be_exported(api):
    agent = make_agent(api, "Bot", "v1 prompt")
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="v2 prompt"))

    old = export(api, "agent", agent, version=1)
    current = export(api, "agent", agent)

    assert [a["base_prompt"] for a in old["agents"].values()] == ["v1 prompt"]
    assert [a["base_prompt"] for a in current["agents"].values()] == ["v2 prompt"]
    assert api.get(f"/api/agents/{agent['id']}/export", params={"version": 9}).status_code == 404


def test_a_workflow_export_holds_each_agent_skill_and_nested_workflow_once(api):
    skill = make_skill(api, "Shared", "Be kind.")
    writer = make_agent(api, "Writer", skills=[skill])
    editor = make_agent(api, "Editor", skills=[skill])
    inner = make_workflow(api, "Inner", [agent_node("w", writer)])
    outer = make_workflow(
        api,
        "Outer",
        [
            agent_node("a", writer),
            agent_node("b", writer),
            agent_node("c", editor),
            workflow_node("i", inner),
        ],
        [("a", "b"), ("b", "c"), ("c", "i")],
    )

    file = export(api, "workflow", outer)

    assert sorted(a["name"] for a in file["agents"].values()) == ["Editor", "Writer"]
    assert [s["name"] for s in file["skills"].values()] == ["Shared"]
    assert sorted(w["name"] for w in file["workflows"].values()) == ["Inner", "Outer"]
    root = file["workflows"][file["root"]["ref"]]
    assert file["root"]["kind"] == "workflow"
    nodes = {n["key"]: n for n in root["nodes"]}
    assert nodes["a"]["ref"] == nodes["b"]["ref"]
    assert nodes["a"]["kind"] == "agent"
    assert file["workflows"][nodes["i"]["ref"]]["name"] == "Inner"
    assert root["edges"] == [
        {"source": "a", "target": "b"},
        {"source": "b", "target": "c"},
        {"source": "c", "target": "i"},
    ]


def test_participants_cant_export_or_import(api, user, participant):  # noqa: F811
    agent = make_agent(api, "Bot")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])
    file = export(api, "agent", agent)

    with signed_in_as(participant):
        assert api.get(f"/api/agents/{agent['id']}/export").status_code == 403
        assert api.get(f"/api/workflows/{wf['id']}/export").status_code == 403
        assert upload(api, file).status_code == 403
        assert upload(api, file, dry_run=True).status_code == 403


# Imports.


def test_a_round_trip_between_organisations_makes_unconnected_copies(api, user, other):
    skill = make_skill(api, "Haiku", "5-7-5", "Poems")
    agent = tool_agent(api, "Poet", skills=[skill], description="Writes poems")
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="v2", tools=[WEB]))
    file = export(api, "agent", agent)

    with signed_in_as(other):
        result = take(api, file, name="poet.json")
        assert result["dry_run"] is False
        root = result["root"]
        assert (root["kind"], root["imported_name"], root["renamed"]) == ("agent", "Poet", False)
        copy = api.get(f"/api/agents/{root['id']}").json()
        v = copy["current_version"]
        assert (copy["name"], copy["description"]) == ("Poet", "Writes poems")
        assert (v["version"], v["base_prompt"], v["note"]) == (1, "v2", "Imported from poet.json")
        assert [(t["key"], t["config"]) for t in v["tools"]] == [
            ("web_search", agent["current_version"]["tools"][0]["config"])
        ]
        assert [(s["name"], s["version"]) for s in v["skills"]] == [("Haiku", 1)]
        assert v["unrunnable"] is None
        # Exporting the copy gives back what was exported.
        again = export(api, "agent", copy)
        for key in ("agents", "skills", "workflows"):
            assert list(again[key].values()) == list(file[key].values())
        # Editing the copy leaves the original alone.
        api.put(f"/api/agents/{copy['id']}", json=agent_body(copy, base_prompt="theirs"))

    mine = api.get(f"/api/agents/{agent['id']}").json()
    assert mine["current_version"]["base_prompt"] == "v2"
    api.put(f"/api/agents/{agent['id']}", json=agent_body(mine, base_prompt="ours"))
    with signed_in_as(other):
        theirs = api.get(f"/api/agents/{root['id']}").json()
        assert theirs["current_version"]["base_prompt"] == "theirs"


def test_clashing_names_get_imported_and_skills_are_always_copied(api):
    skill = make_skill(api, "Haiku", "5-7-5")
    agent = make_agent(api, "Poet", skills=[skill])
    file = export(api, "agent", agent)

    first = take(api, file)
    second = take(api, file)

    assert first["root"]["imported_name"] == "Poet (imported)"
    assert second["root"]["imported_name"] == "Poet (imported 2)"
    assert [(i["kind"], i["imported_name"], i["renamed"]) for i in second["items"]] == [
        ("skill", "Haiku (imported 2)", True),
        ("agent", "Poet (imported 2)", True),
    ]
    assert names(api, "skills") == ["Haiku", "Haiku (imported 2)", "Haiku (imported)"]
    # Each copy pins its own copy of the skill, never the organisation's own.
    copy = api.get(f"/api/agents/{first['root']['id']}").json()
    (pinned,) = copy["current_version"]["skills"]
    assert pinned["skill_id"] != skill["id"]
    assert pinned["name"] == "Haiku (imported)"


def test_an_agent_used_twice_is_created_once_and_nested_workflows_import_in_full(api, other):
    skill = make_skill(api, "Shared", "Be kind.")
    writer = make_agent(api, "Writer", skills=[skill])
    inner = make_workflow(api, "Inner", [agent_node("w", writer)])
    outer = make_workflow(
        api,
        "Outer",
        [agent_node("a", writer), agent_node("b", writer), workflow_node("i", inner)],
        [("a", "b"), ("b", "i")],
    )
    file = export(api, "workflow", outer)

    with signed_in_as(other):
        result = take(api, file)
        assert sorted((i["kind"], i["name"]) for i in result["items"]) == [
            ("agent", "Writer"),
            ("skill", "Shared"),
            ("workflow", "Inner"),
            ("workflow", "Outer"),
        ]
        assert names(api, "agents") == ["Writer"]
        made = api.get(f"/api/workflows/{result['root']['id']}").json()
        nodes = {n["key"]: n for n in made["current_version"]["nodes"]}
        assert nodes["a"]["version_id"] == nodes["b"]["version_id"]
        assert nodes["a"]["name"] == "Writer"
        assert nodes["i"]["name"] == "Inner"
        inner_copy = api.get(f"/api/workflows/{nodes['i']['target_id']}").json()
        (w,) = inner_copy["current_version"]["nodes"]
        assert w["version_id"] == nodes["a"]["version_id"]
        assert [(e["source"], e["target"]) for e in made["current_version"]["edges"]] == [
            ("a", "b"),
            ("b", "i"),
        ]


def test_an_exported_organisation_assistant_imports_as_an_ordinary_agent(api, other):
    chat = api.post("/api/chats", json={"name": "general"})
    assert chat.status_code == 201, chat.text
    (assistant,) = [a for a in api.get("/api/agents").json()]
    file = export(api, "agent", assistant)

    with signed_in_as(other):
        api.post("/api/chats", json={"name": "general"})
        result = take(api, file)

    assert result["root"]["imported_name"] == f"{assistant['name']} (imported)"
    with user_store(other) as store:
        builtins = store.query(Agent.name, Agent.builtin).order_by(Agent.id).all()
    assert [b for _, b in builtins] == ["assistant", None]


def test_a_missing_tool_leaves_the_version_unrunnable_with_the_reason(api):
    agent = tool_agent(api, "Searcher")
    file = export(api, "agent", agent)
    (exported,) = file["agents"].values()
    exported["tools"].append({"key": "crm_connector", "settings": {"connector": "Salesforce"}})

    result = take(api, file)

    reason = result["root"]["unrunnable"]
    assert "crm_connector" in reason
    copy = api.get(f"/api/agents/{result['root']['id']}").json()
    assert copy["current_version"]["unrunnable"] == reason
    assert api.get(f"/api/agents/{copy['id']}/versions/1").json()["unrunnable"] == reason
    refused = api.post(f"/api/agents/{copy['id']}/runs")
    assert refused.status_code == 409
    assert "crm_connector" in refused.json()["detail"]


def test_a_workflow_running_an_unrunnable_agent_says_so_and_refuses_to_start(api):
    agent = make_agent(api, "Bot")
    wf = make_workflow(api, "Flow", [agent_node("a", agent)])
    file = export(api, "workflow", wf)
    (exported,) = file["agents"].values()
    exported["tools"] = [{"key": "gone_tool", "settings": {}}]

    result = take(api, file)

    assert "Bot (imported)" in result["root"]["unrunnable"]
    made = api.get(f"/api/workflows/{result['root']['id']}").json()
    assert "gone_tool" in made["current_version"]["unrunnable"]
    resp = api.post(f"/api/workflows/{made['id']}/runs", json={"input": "go"})
    assert resp.status_code == 409


def test_a_dry_run_reports_what_would_happen_and_writes_nothing(api, user):
    skill = make_skill(api, "Haiku")
    agent = make_agent(api, "Poet", skills=[skill])
    file = export(api, "agent", agent)
    (exported,) = file["agents"].values()
    exported["tools"] = [{"key": "gone_tool", "settings": {}}]
    log_before = api.get("/api/audit-log").json()["entries"]

    result = take(api, file, dry_run=True)

    assert result["dry_run"] is True
    assert [
        (i["kind"], i["imported_name"], i["renamed"], i["id"], bool(i["unrunnable"]))
        for i in result["items"]
    ] == [
        ("skill", "Haiku (imported)", True, None, False),
        ("agent", "Poet (imported)", True, None, True),
    ]
    assert names(api, "agents") == ["Poet"]
    assert names(api, "skills") == ["Haiku"]
    assert api.get("/api/audit-log").json()["entries"] == log_before


@pytest.mark.parametrize(
    "data",
    [
        b"not json at all",
        b"[]",
        json.dumps({"format": "something-else", "format_version": 1}).encode(),
        json.dumps(
            {
                "format": "aios-export",
                "format_version": 1,
                "exported_at": "2026-01-01T00:00:00Z",
                "root": {"kind": "agent", "ref": "agent-1"},
                "agents": {"agent-1": {"name": "Bot", "skills": ["skill-9"]}},
            }
        ).encode(),
    ],
    ids=["text", "list", "other format", "dangling reference"],
)
def test_a_file_that_isnt_an_export_is_a_422_and_creates_nothing(api, data):
    resp = upload(api, data)

    assert resp.status_code == 422, resp.text
    assert "isn't an export" in resp.json()["detail"]
    assert names(api, "agents") == []


def test_a_file_thats_too_large_is_a_413_and_creates_nothing(api, monkeypatch):
    file = export(api, "agent", make_agent(api, "Bot"))
    monkeypatch.setattr(get_settings(), "export_max_bytes", 100)

    resp = upload(api, file)

    assert resp.status_code == 413, resp.text
    assert names(api, "agents") == ["Bot"]


def test_exports_and_imports_are_audited(api, user, other):
    agent = make_agent(api, "Bot")
    file = export(api, "agent", agent)
    with signed_in_as(other):
        result = take(api, file, name="bot.json")
        with_role = api.get("/api/audit-log").json()["entries"]
    exported = api.get("/api/audit-log", params={"action": "agent.exported"}).json()["entries"]

    assert [(e["action"], e["target_id"], e["details"]) for e in exported] == [
        ("agent.exported", str(agent["id"]), {"name": "Bot", "version": 1})
    ]
    imported = [e for e in with_role if e["action"] == "agent.imported"]
    assert [(e["target_id"], e["details"]) for e in imported] == [
        (str(result["root"]["id"]), {"name": "Bot", "file": "bot.json"})
    ]
