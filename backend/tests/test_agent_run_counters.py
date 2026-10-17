"""Agent runs record how they went and what they cost: status, times, tool calls and tokens,
written from the turn loop at each model step (see `aios.api.agents.turn_stream`)."""

from datetime import datetime

from fake_model import ToolCall, Usage, model, use_model
from langchain_core.messages import AIMessage
from test_agents import agent_body, chat_body, make_agent

from aios.api.agents import tool_call_count
from aios.tools import hybrid_search


def searching_agent(api, monkeypatch) -> tuple[dict, str]:
    """An agent with the document search tool, whose searches answer at once; and its runs' URL."""
    agent = make_agent(api, "Bot")
    tool = {"key": "hybrid_search", "config": {}}
    resp = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=[tool]))
    assert resp.status_code == 200, resp.text

    async def search(owner_id, query, limit):
        return [{"source": f"{query}.md", "headings": [], "text": query}]

    monkeypatch.setattr(hybrid_search, "document_search_results", search)
    return agent, f"/api/agents/{agent['id']}/runs"


def counters(run: dict) -> dict:
    return {k: run[k] for k in ("status", "tool_call_count", "input_tokens", "output_tokens")}


def test_a_new_run_has_nothing_tracked_yet(api):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    assert counters(run) == {
        "status": None,
        "tool_call_count": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    assert (run["started_at"], run["finished_at"]) == (None, None)


def test_a_turn_with_tool_calls_records_its_counters_and_outcome(api, monkeypatch):
    agent, runs_url = searching_agent(api, monkeypatch)
    run_id = api.post(runs_url).json()["id"]

    async def script(messages, info):
        if messages[-1].type == "human":
            yield Usage(100, 10)
            yield ToolCall("search_documents", {"query": "apples"})
            yield ToolCall("search_documents", {"query": "pears"})
        else:
            # Each model call is billed for its whole context again: the counters add them up.
            yield "Found both."
            yield Usage(250, 5)

    with use_model(model(script)):
        resp = api.post(f"{runs_url}/{run_id}/chat", json=chat_body("Search for fruit"))
        assert resp.status_code == 200

    run = api.get(f"{runs_url}/{run_id}").json()
    assert counters(run) == {
        "status": "succeeded",
        "tool_call_count": 2,
        "input_tokens": 350,
        "output_tokens": 15,
    }
    started, finished = (datetime.fromisoformat(run[k]) for k in ("started_at", "finished_at"))
    assert started <= finished
    # The run list shows the same.
    (listed,) = api.get(runs_url).json()
    assert counters(listed) == counters(run)
    assert (listed["started_at"], listed["finished_at"]) == (run["started_at"], run["finished_at"])


def test_a_run_is_running_from_when_its_turn_begins(api):
    agent = make_agent(api, "Bot")
    runs_url = f"/api/agents/{agent['id']}/runs"
    run_id = api.post(runs_url).json()["id"]
    seen = []

    async def script(messages, info):
        seen.append(api.get(f"{runs_url}/{run_id}").json())
        yield "hi"

    with use_model(model(script)):
        api.post(f"{runs_url}/{run_id}/chat", json=chat_body("hello"))

    (mid_turn,) = seen
    assert mid_turn["status"] == "running"
    assert mid_turn["started_at"] is not None and mid_turn["finished_at"] is None
    after = api.get(f"{runs_url}/{run_id}").json()
    assert after["status"] == "succeeded"
    assert after["started_at"] == mid_turn["started_at"]


def test_a_failed_turn_is_marked_failed_when_it_ends(api):
    agent = make_agent(api, "Bot")
    runs_url = f"/api/agents/{agent['id']}/runs"
    run_id = api.post(runs_url).json()["id"]

    async def script(messages, info):
        yield Usage(40, 2)
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(script)):
        api.post(f"{runs_url}/{run_id}/chat", json=chat_body("hello"))

    run = api.get(f"{runs_url}/{run_id}").json()
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    # The call never finished, so its usage never arrived: nothing was counted.
    assert (run["tool_call_count"], run["input_tokens"], run["output_tokens"]) == (None,) * 3


def test_counters_add_up_over_a_runs_turns(api, monkeypatch):
    agent, runs_url = searching_agent(api, monkeypatch)
    run_id = api.post(runs_url).json()["id"]

    async def script(messages, info):
        if messages[-1].type == "human":
            yield Usage(10, 1)
            yield ToolCall("search_documents", {"query": messages[-1].content})
        else:
            yield "Done."
            yield Usage(20, 2)

    with use_model(model(script)):
        api.post(f"{runs_url}/{run_id}/chat", json=chat_body("first"))
        first = api.get(f"{runs_url}/{run_id}").json()
        api.post(f"{runs_url}/{run_id}/chat", json=chat_body("second"))
        second = api.get(f"{runs_url}/{run_id}").json()

    assert counters(first) == {
        "status": "succeeded",
        "tool_call_count": 1,
        "input_tokens": 30,
        "output_tokens": 3,
    }
    assert counters(second) == {
        "status": "succeeded",
        "tool_call_count": 2,
        "input_tokens": 60,
        "output_tokens": 6,
    }
    # The times are the latest turn's.
    assert second["started_at"] > first["finished_at"]


def test_tool_calls_include_the_providers_own():
    # A web search the provider ran, then a call the agent runs: both are tool calls, as the
    # run page's event log counts them.
    response = AIMessage(
        content=[
            {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": []},
            {"type": "text", "text": "Let me check the documents too."},
        ],
        tool_calls=[{"name": "search_documents", "args": {"query": "x"}, "id": "call_1"}],
    )
    assert tool_call_count(response) == 2
    assert tool_call_count(AIMessage("Just text.")) == 0
