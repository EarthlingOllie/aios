from dataclasses import replace

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.native_tools import WebSearchTool
from test_agents import agent_body, chat_body, make_agent, spy_run_stream

from aios.models import Agent
from aios.runner import runner
from aios.tools import TOOLS
from aios.tools.web_search import WebSearchConfig
from aios.versioning import config_hash, save_agent_version

WEB = {"key": "web_search", "config": {}}


def test_every_tool_has_default_settings():
    for spec in TOOLS.values():
        spec.build(spec.config_model())


def test_web_search_settings():
    config = WebSearchConfig(
        allowed_domains=[" Example.com ", "", "example.com", "docs.python.org"]
    )
    assert config.allowed_domains == ["example.com", "docs.python.org"]
    paths = ["example.com/blog", "example.com/*/articles"]
    assert WebSearchConfig(blocked_domains=paths).blocked_domains == paths
    for bad in ["https://docs.python.org/3/", "python org", "*.example.com", "localhost"]:
        with pytest.raises(ValidationError, match="isn't a domain"):
            WebSearchConfig(allowed_domains=[bad])
    with pytest.raises(ValidationError, match="not both"):
        WebSearchConfig(allowed_domains=["a.com"], blocked_domains=["b.com"])
    with pytest.raises(ValidationError):
        WebSearchConfig(max_uses=0)
    with pytest.raises(ValidationError):
        WebSearchConfig(unknown=1)


def test_tool_catalog(api):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    web = tools["web_search"]
    assert web["default_config"] == {"max_uses": 5, "allowed_domains": [], "blocked_domains": []}
    assert web["config_schema"]["properties"]["max_uses"]["maximum"] == 20


def test_tools_are_versioned_with_the_agent(api):
    agent = make_agent(api, "Researcher")
    url = f"/api/agents/{agent['id']}"
    assert agent["current_version"]["tools"] == []

    v2 = api.put(url, json=agent_body(agent, tools=[WEB])).json()["current_version"]
    assert v2["version"] == 2
    assert v2["tools"] == [
        {
            "key": "web_search",
            "name": "Web search",
            "description": TOOLS["web_search"].description,
            # Stored with defaults filled in, so an explicit default is the same config.
            "config": {"max_uses": 5, "allowed_domains": [], "blocked_domains": []},
            "available": True,
        }
    ]
    explicit = {"key": "web_search", "config": {"max_uses": 5}}
    agent = api.get(url).json()
    assert (
        api.put(url, json=agent_body(agent, tools=[explicit])).json()["current_version"]["version"]
        == 2
    )
    assert [(a["name"], a["tool_count"]) for a in api.get("/api/agents").json()] == [
        ("Researcher", 1)
    ]

    # Changing a tool's settings, or removing it, creates a version.
    limited = {"key": "web_search", "config": {"max_uses": 2, "allowed_domains": ["a.com"]}}
    v3 = api.put(url, json=agent_body(agent, tools=[limited])).json()["current_version"]
    assert (v3["version"], v3["tools"][0]["config"]["max_uses"]) == (3, 2)
    v4 = api.put(url, json=agent_body(agent, tools=[])).json()["current_version"]
    assert (v4["version"], v4["tools"]) == (4, [])

    restored = api.post(f"{url}/versions/3/restore").json()["current_version"]
    assert (restored["version"], restored["tools"]) == (5, v3["tools"])
    assert api.get(f"{url}/versions/2").json()["tools"] == v2["tools"]


def test_invalid_tools_are_rejected(api):
    agent = make_agent(api, "Bot")
    url = f"/api/agents/{agent['id']}"
    resp = api.put(url, json=agent_body(agent, tools=[{"key": "nope"}]))
    assert (resp.status_code, resp.json()["detail"]) == (422, "Unknown tool: nope")
    assert api.put(url, json=agent_body(agent, tools=[WEB, WEB])).status_code == 422
    both = {"allowed_domains": ["a.com"], "blocked_domains": ["b.com"]}
    resp = api.put(url, json=agent_body(agent, tools=[{"key": "web_search", "config": both}]))
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Invalid settings for Web search: set either allowed or blocked domains, not both"
    )
    url_entry = {"allowed_domains": ["https://docs.python.org/3/"]}
    resp = api.put(url, json=agent_body(agent, tools=[{"key": "web_search", "config": url_entry}]))
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Invalid settings for Web search: allowed_domains: 'https://docs.python.org/3/' isn't a"
        " domain; use one like example.com or example.com/blog, without https://"
    )
    assert api.get(url).json()["current_version"]["version"] == 1


class DriftedWebSearchConfig(BaseModel):
    # The settings model after a release that dropped `max_uses` and added `safe_search`.
    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str] = []
    blocked_domains: list[str] = []
    safe_search: bool = True


def test_stored_settings_survive_settings_model_changes(api, monkeypatch):
    agent = make_agent(api, "Researcher")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[WEB])).json()
    stored = agent["current_version"]["tools"][0]["config"]
    spec = replace(TOOLS["web_search"], config_model=DriftedWebSearchConfig)
    monkeypatch.setitem(TOOLS, "web_search", spec)

    # Sending the stored settings back unchanged, as the UI does, keeps them as they are, so
    # renaming neither creates a version nor fails validation.
    unchanged = [{"key": "web_search", "config": stored}]
    resp = api.put(url, json=agent_body(agent, name="Renamed", tools=unchanged))
    assert resp.status_code == 200
    version = resp.json()["current_version"]
    assert (version["version"], version["tools"][0]["config"]) == (2, stored)

    # Changed settings are validated against the current model.
    changed = [{"key": "web_search", "config": {**stored, "allowed_domains": ["a.com"]}}]
    assert api.put(url, json=agent_body(agent, tools=changed)).status_code == 422
    edited = [{"key": "web_search", "config": {"allowed_domains": ["a.com"]}}]
    version = api.put(url, json=agent_body(agent, tools=edited)).json()["current_version"]
    assert (version["version"], version["tools"][0]["config"]) == (
        3,
        {"allowed_domains": ["a.com"], "blocked_domains": [], "safe_search": True},
    )


def test_versions_without_tools_keep_their_hash(db, user):
    agent = Agent(owner_id=user.id, name="Old")
    db.add(agent)
    version = save_agent_version(
        db, agent, base_prompt="Hi.", skills=[], note=None, user_id=user.id
    )
    db.commit()
    # The hash a version got before tools existed, so unchanged re-saves still match it.
    old = config_hash({"base_prompt": "Hi.", "model": None, "skill_version_ids": []})
    assert version.config_hash == old
    db.delete(agent)
    db.commit()


def test_run_is_given_its_versions_tools(api, monkeypatch):
    tool = {"key": "web_search", "config": {"max_uses": 3, "allowed_domains": ["python.org"]}}
    agent = make_agent(api, "Searcher")
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool])).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    # Removing the tool from the agent doesn't take it away from a run already pinned to v2.
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[]))

    seen = spy_run_stream(monkeypatch)
    native_tools = []

    async def model(messages, info: AgentInfo):
        native_tools.extend(info.model_request_parameters.native_tools)
        yield "ok"

    with runner.override(model=FunctionModel(stream_function=model)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("search")).status_code == 200
    assert native_tools == [
        WebSearchTool(max_uses=3, allowed_domains=["python.org"], blocked_domains=None)
    ]
    assert seen["metadata"]["tools"] == [
        {
            "key": "web_search",
            "config": {"max_uses": 3, "allowed_domains": ["python.org"], "blocked_domains": []},
        }
    ]


def test_tool_removed_from_the_library(api, monkeypatch):
    agent = make_agent(api, "Searcher")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[WEB])).json()
    run = api.post(f"{url}/runs").json()
    api.put(url, json=agent_body(agent, tools=[]))
    monkeypatch.delitem(TOOLS, "web_search")

    # Old versions still show it, but can't be restored or run.
    shown = api.get(f"{url}/versions/2").json()["tools"][0]
    assert (shown["name"], shown["available"]) == ("web_search", False)
    assert api.post(f"{url}/versions/2/restore").status_code == 409
    assert api.post(f"{url}/runs/{run['id']}/chat", json=chat_body("hi")).status_code == 409
