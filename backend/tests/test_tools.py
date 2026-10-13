from dataclasses import replace

import pytest
from fake_model import ToolCall, last_tool_results, model, tool_output, use_model
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from test_agents import agent_body, chat_body, make_agent, spy_run, text_deltas
from test_search import unit

from aios import llm, search
from aios.agent import ChatDeps
from aios.config import get_settings
from aios.models import Agent, Chunk, Document, DocumentStatus, User
from aios.runner import server_tools
from aios.tools import TOOLS, ToolSpec, register
from aios.tools.hybrid_search import HybridSearchConfig
from aios.tools.web_search import WEB_SEARCH, WebSearchConfig
from aios.versioning import config_hash, save_agent_version

WEB = {"key": "web_search", "config": {}}


def test_every_tool_has_default_settings():
    for spec in TOOLS.values():
        spec.build(spec.config_model())


def test_tool_keys_are_unique():
    spec = TOOLS["web_search"]
    with pytest.raises(ValueError, match="already registered"):
        register(ToolSpec(spec.key, "Other", "", spec.config_model, spec.build))
    assert TOOLS["web_search"] is spec


def test_web_search_settings():
    config = WebSearchConfig(
        allowed_domains=[" Example.com ", "", "example.com", "docs.python.org"]
    )
    assert config.allowed_domains == ["example.com", "docs.python.org"]
    paths = ["example.com/blog", "example.com/*/articles"]
    assert WebSearchConfig(blocked_domains=paths).blocked_domains == paths
    # Hostnames are case-insensitive, paths aren't.
    assert WebSearchConfig(allowed_domains=["GitHub.com/MyOrg/Repo"]).allowed_domains == [
        "github.com/MyOrg/Repo"
    ]
    for bad in ["https://docs.python.org/3/", "python org", "*.example.com", "localhost"]:
        with pytest.raises(ValidationError, match="isn't a domain"):
            WebSearchConfig(allowed_domains=[bad])
    with pytest.raises(ValidationError, match="not both"):
        WebSearchConfig(allowed_domains=["a.com"], blocked_domains=["b.com"])
    with pytest.raises(ValidationError):
        WebSearchConfig(max_uses=0)
    with pytest.raises(ValidationError):
        WebSearchConfig(unknown=1)


def test_hybrid_search_settings():
    assert HybridSearchConfig().max_results == 8
    assert HybridSearchConfig(max_results=50).max_results == 50
    for bad in [0, 51]:
        with pytest.raises(ValidationError):
            HybridSearchConfig(max_results=bad)
    with pytest.raises(ValidationError):
        HybridSearchConfig(unknown=1)


def test_tool_catalog(api):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    web = tools["web_search"]
    assert web["default_config"] == {"max_uses": 5, "allowed_domains": [], "blocked_domains": []}
    assert web["config_schema"]["properties"]["max_uses"]["maximum"] == 20
    hybrid = tools["hybrid_search"]
    assert (hybrid["name"], hybrid["default_config"]) == ("Document search", {"max_results": 8})
    assert hybrid["config_schema"]["properties"]["max_results"]["maximum"] == 50


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


def drifted_build(config: DriftedWebSearchConfig) -> list:
    return [{"type": WEB_SEARCH, "name": "web_search", "allowed_domains": config.allowed_domains}]


def test_stored_settings_load_like_a_run_loads_them(api, monkeypatch):
    agent = make_agent(api, "Researcher")
    url = f"/api/agents/{agent['id']}"
    agent = api.put(url, json=agent_body(agent, tools=[WEB])).json()
    stored = agent["current_version"]["tools"]
    api.put(url, json=agent_body(agent, tools=[]))
    spec = replace(TOOLS["web_search"], config_model=DriftedWebSearchConfig, build=drifted_build)
    monkeypatch.setitem(TOOLS, "web_search", spec)

    # A dropped field is ignored when a version runs, and on restore, which keeps the settings
    # as stored rather than filling in the new model's defaults.
    run = api.post(f"{url}/runs").json()
    assert run["agent_version"] == 3
    restored = api.post(f"{url}/versions/2/restore").json()["current_version"]
    assert (restored["version"], restored["tools"]) == (4, stored)
    assert api.post(f"{url}/runs").status_code == 201

    # Settings the current model rejects are refused on save and restore, not saved unrunnable.
    class StricterConfig(DriftedWebSearchConfig):
        max_uses: int = Field(3, le=3)

    monkeypatch.setitem(TOOLS, "web_search", replace(spec, config_model=StricterConfig))
    agent = api.get(url).json()
    unchanged = [{"key": t["key"], "config": t["config"]} for t in stored]
    resp = api.put(url, json=agent_body(agent, name="Renamed", tools=unchanged))
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Invalid settings for Web search: max_uses")
    resp = api.post(f"{url}/versions/2/restore")
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("That version can't be restored: Invalid settings")
    assert api.get(url).json()["name"] == "Researcher"


def test_version_history_counts_tools(api):
    agent = make_agent(api, "Researcher")
    url = f"/api/agents/{agent['id']}"
    api.put(url, json=agent_body(agent, tools=[WEB]))
    counts = [(v["version"], v["tool_count"]) for v in api.get(f"{url}/versions").json()]
    assert counts == [(2, 1), (1, 0)]


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

    seen = spy_run(monkeypatch)
    native_tools = []

    async def script(messages, info):
        native_tools.extend(info.server_tools)
        yield "ok"

    with use_model(model(script)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("search")).status_code == 200
    # Anthropic's server-side web search, in the version the default model supports.
    assert native_tools == [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 3,
            "allowed_domains": ["python.org"],
        }
    ]
    assert seen["metadata"]["tools"] == [
        {
            "key": "web_search",
            "config": {"max_uses": 3, "allowed_domains": ["python.org"], "blocked_domains": []},
        }
    ]


def test_web_search_version_follows_the_model():
    assert llm.web_search_type("anthropic:claude-sonnet-5-5") == "web_search_20260209"
    assert llm.web_search_type("anthropic:claude-opus-4-6") == "web_search_20260209"
    assert llm.web_search_type("anthropic:claude-haiku-4-5") == "web_search_20250305"
    tools = TOOLS["web_search"].build(WebSearchConfig(blocked_domains=["a.com"]))
    assert server_tools(tools, "anthropic:claude-haiku-4-5") == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "blocked_domains": ["a.com"],
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


def test_no_new_run_while_the_current_version_uses_a_removed_tool(api, monkeypatch):
    agent = make_agent(api, "Searcher")
    url = f"/api/agents/{agent['id']}"
    api.put(url, json=agent_body(agent, tools=[WEB]))
    monkeypatch.delitem(TOOLS, "web_search")

    # Refused up front, rather than leaving a run whose first message would get the 409.
    resp = api.post(f"{url}/runs")
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "This agent version can't be run: Unknown tool: web_search",
    )
    assert api.get(f"{url}/runs").json() == []


@pytest.fixture
def documents(db, user):
    """Three chunks of `user`'s, plus one of another user's that matches the same query."""
    other = User(github_id=-2, login="other-user")
    db.add(other)
    db.flush()
    docs = [
        Document(owner_id=user.id, filename="fish.md", data=b"", status=DocumentStatus.ready),
        Document(owner_id=other.id, filename="theirs.md", data=b"", status=DocumentStatus.ready),
    ]
    texts = [
        ["Zebrafish are popular in genetics labs.", "The zebrafish genome was sequenced."],
        ["Their zebrafish notes."],
    ]
    for doc, chunks in zip(docs, texts, strict=True):
        doc.chunks = [
            Chunk(
                ordinal=i, text=t, contextualized=t, meta={"headings": ["Fish"]}, embedding=unit(0)
            )
            for i, t in enumerate(chunks)
        ]
    docs[0].chunks.append(
        Chunk(ordinal=2, text="Cats purr.", contextualized="Cats purr.", meta={}, embedding=unit(1))
    )
    db.add_all(docs)
    db.commit()
    yield
    db.delete(docs[0])
    db.delete(other)  # and, by the database's cascade, their document
    db.commit()


def search_once(query: str):
    """A model that searches once with `query`, then replies with what came back."""
    seen = {}

    async def script(messages, info):
        if returns := last_tool_results(messages):
            seen["results"] = tool_output(returns[0])
            yield f"found {len(seen['results'])}"
        else:
            seen["tools"] = list(info.tools)
            seen["description"] = info.tools["search_documents"].description
            yield ToolCall("search_documents", {"query": query})

    return model(script), seen


def test_hybrid_search_tool_searches_the_users_documents(api, user, documents, monkeypatch):
    queries = []

    async def embed_query(text):
        queries.append(text)
        return unit(0)

    monkeypatch.setattr(search, "embed_query", embed_query)
    tool = {"key": "hybrid_search", "config": {"max_results": 2}}
    agent = make_agent(api, "Librarian")
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool])).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    spy = spy_run(monkeypatch)
    fake, seen = search_once("zebrafish")
    with use_model(fake):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        resp = api.post(url, json=chat_body("what about zebrafish?"))
    assert resp.status_code == 200
    assert spy["deps"] == ChatDeps(user_id=user.id, run_id=run["id"])
    assert seen["tools"] == ["search_documents"]
    assert "hybrid (semantic + keyword)" in seen["description"]
    assert queries == ["zebrafish"]
    # Only this user's documents, best match first, capped at the tool's `max_results`.
    assert seen["results"] == [
        {"source": "fish.md", "headings": ["Fish"], "text": t}
        for t in ["Zebrafish are popular in genetics labs.", "The zebrafish genome was sequenced."]
    ]
    assert text_deltas(resp) == "found 2"
    # The tool call and its results are saved with the run.
    detail = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    assert [m["type"] for m in detail["messages"]] == ["human", "ai", "tool", "ai"]
    assert detail["messages"][1]["tool_calls"][0]["name"] == "search_documents"
    tool_part = next(p for p in detail["ui_messages"][1]["parts"] if p["type"].startswith("tool-"))
    assert (tool_part["type"], tool_part["state"]) == ("tool-search_documents", "output-available")
    assert tool_part["output"] == seen["results"]


def test_hybrid_search_tool_ignores_blank_queries(api, monkeypatch):
    # A blank query returns nothing without embedding it (or failing for lack of an API key).
    monkeypatch.setattr(search, "embed_query", None)
    agent = make_agent(api, "Librarian")
    tools = [{"key": "hybrid_search", "config": {}}]
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    fake, seen = search_once("  ")
    with use_model(fake):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert text_deltas(api.post(url, json=chat_body("hi"))) == "found 0"
    assert seen["results"] == []


def test_hybrid_search_fills_a_limit_above_its_candidates(db, user, documents, monkeypatch):
    # A query with no keyword matches, so every result comes from the semantic branch.
    monkeypatch.setattr(get_settings(), "search_candidates", 1)
    hits = search.hybrid_search_with_vector(
        db, owner_id=user.id, query="xylophone", query_vector=unit(0), limit=3
    )
    assert len(hits) == 3
