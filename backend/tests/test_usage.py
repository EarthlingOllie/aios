"""The usage ledger: every billable call writes one row, priced in GBP from the price table and
charged to its agent or workflow, run, turn, invocation, person and cause. Admins read totals;
agents read the rows through `reporting.usage`.

Through the API, as a signed-in member, with the scripted model."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import test_workflow_runs as workflow_runs
from conftest import join_organisation, store_of, take_turn
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.messages import AIMessage
from test_agents import make_agent
from test_chat_agent_invocations import say
from test_chats import add, make_chat, signed_in_as
from test_data_tools import data_agent, run_tools, store  # noqa: F401 (fixtures)
from test_run_python import backend, chat, python_agent  # noqa: F401 (a fixture)
from test_workflow_runs import queued  # noqa: F401 (a fixture)
from test_workflows import agent_node, make_workflow

from aios import embeddings, search_index, usage
from aios.config import get_settings
from aios.datastores.models import Turn
from aios.testing.fake_model import ToolCall, Usage, model, use_handoff, use_model

MODEL = "anthropic:claude-sonnet-5-5"
EMBEDDER = "voyageai:voyage-4"


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """Round prices: £2 and £10 per million input and output tokens, £0.01 a web search, £1 per
    million embedded tokens, £0.001 per sandbox second."""
    table = {
        MODEL: {"input": 2.0, "output": 10.0, "web_search": 0.01},
        EMBEDDER: {"input": 1.0},
        "sandbox": {"per_second": 0.001},
    }
    settings = get_settings()
    monkeypatch.setattr(settings, "price_table", table)
    monkeypatch.setattr(settings, "llm_model", MODEL)
    monkeypatch.setattr(settings, "embedding_model", EMBEDDER)
    return table


def totals(api, group: str = "agent", **params) -> dict:
    resp = api.get("/api/usage", params={"group": group, **params})
    assert resp.status_code == 200, resp.text
    return {t["key"]: t for t in resp.json()["totals"]}


def three_calls():
    """A model that calls a tool twice, then answers: three calls, 100 and 10 tokens each."""

    async def script(messages, info):
        yield Usage(100, 10)
        if sum(m.type == "tool" for m in messages) < 2:
            yield ToolCall("describe_data", {})
        else:
            yield "Done."

    return model(script)


def test_a_turn_with_n_model_calls_writes_n_rows_priced_in_gbp(api, user):
    agent = make_agent(api, "Counter")
    api.put(
        f"/api/agents/{agent['id']}",
        json={"name": "Counter", "tools": [{"key": "describe_data", "config": {}}]},
    )
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(three_calls()):
        take_turn(api, agent["id"], run["id"], "go")

    mine = totals(api)[agent["id"]]
    assert mine["name"] == "Counter"
    assert (mine["model_calls"], mine["input_tokens"], mine["output_tokens"]) == (3, 300, 30)
    # 300 tokens in at £2 a million, 30 out at £10.
    assert mine["cost"] == pytest.approx(300 * 2 / 1e6 + 30 * 10 / 1e6)
    assert mine["unpriced"] == 0
    assert api.get("/api/usage").json()["currency"] == "GBP"
    # Charged to the person who started the run, on the day.
    assert totals(api, "person")[user.id]["model_calls"] == 3
    today = datetime.now(UTC).date().isoformat()
    assert totals(api, "day")[today]["model_calls"] == 3


def test_a_model_with_no_price_records_a_blank_cost(api, prices):
    del prices[MODEL]
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def script(messages, info):
        yield "Hi."
        yield Usage(50, 5)

    with use_model(model(script)):
        take_turn(api, agent["id"], run["id"], "hi")
    # The turn went fine regardless.
    assert api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()["status"] == "succeeded"
    mine = totals(api)[agent["id"]]
    assert (mine["model_calls"], mine["cost"], mine["unpriced"]) == (1, None, 1)


def test_recording_a_model_call_again_never_counts_it_twice(api, user, in_store):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(three_calls()):
        take_turn(api, agent["id"], run["id"], "go")
    turn = in_store.scalars(select_turns(run["id"])).one()
    charge = usage.for_turn(store_of(user), turn.id)
    response = AIMessage("again", id="msg_retried", usage_metadata=tokens(7, 3))

    usage.record_turn_call(charge, MODEL, response)
    usage.record_turn_call(charge, MODEL, response)

    assert totals(api)[agent["id"]]["model_calls"] == 4
    assert in_store.get(Turn, turn.id).model_calls == 4


def select_turns(run_id: int):
    from sqlalchemy import select

    return select(Turn).where(Turn.agent_run_id == run_id)


def tokens(i: int, o: int) -> dict:
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


def test_web_searches_the_provider_ran_write_a_row_of_their_own(api, user):
    agent = make_agent(api, "Researcher")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    response = AIMessage(
        content=[
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
            {"type": "server_tool_use", "id": "s2", "name": "web_search", "input": {}},
            {"type": "text", "text": "Found it."},
        ],
        id="msg_searched",
        usage_metadata=tokens(10, 1),
    )
    with use_model(model(_say("hi"))):
        take_turn(api, agent["id"], run["id"], "go")
    charge = usage.for_turn(store_of(user), _latest_turn(user, run["id"]))
    usage.record_turn_call(charge, MODEL, response)

    mine = totals(api)[agent["id"]]
    assert (mine["model_calls"], mine["web_searches"]) == (2, 2)
    # The turn's own call and this one, 10 tokens in and 1 out each, and two searches.
    assert mine["cost"] == pytest.approx(20 * 2 / 1e6 + 2 * 10 / 1e6 + 2 * 0.01)


def _say(text: str):
    async def script(messages, info):
        yield text
        yield Usage(10, 1)

    return script


def _latest_turn(user, run_id: int) -> int:
    from conftest import user_store

    with user_store(user) as s:
        return s.scalars(select_turns(run_id).order_by(Turn.id.desc())).first().id


def test_the_handoff_call_is_charged_to_the_person_who_mentioned(api, user, data_agent):  # noqa: F811
    bot = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", bot)

    async def handoff_script(messages, info):
        yield Usage(40, 4)
        yield ToolCall("Handoff", {"ack": "On it.", "brief": ""})

    with use_handoff(model(handoff_script)), use_model(model(_say("Done."))):
        say(api, chat, "go", ("agent", bot))

    [row] = [
        r
        for r in reported(api, data_agent)
        if r["purpose"] == "handoff" and r["agent_id"] == bot["id"]
    ]
    assert (row["kind"], row["person_id"], row["cause"]) == ("model_call", user.id, "mention")
    assert (row["input_tokens"], row["output_tokens"]) == (40, 4)
    assert row["invocation_id"] is not None and row["turn_id"] is None
    turn_rows = [
        r
        for r in reported(api, data_agent)
        if r["purpose"] == "turn" and r["agent_id"] == bot["id"]
    ]
    assert [(r["invocation_id"], r["cause"], r["person_id"]) for r in turn_rows] == [
        (row["invocation_id"], "mention", user.id)
    ]


def reported(api, agent) -> list[dict]:
    """`reporting.usage`, as an agent reads it with `query_data`."""
    [result] = run_tools(
        api, agent, ToolCall("query_data", {"sql": "SELECT * FROM reporting.usage ORDER BY 1"})
    )
    return [dict(zip(result["columns"], row, strict=True)) for row in result["rows"]]


def test_agents_read_the_ledger_through_a_reporting_view_without_any_text(api, data_agent):  # noqa: F811
    secret = "Codename Bluebird"

    async def script(messages, info):
        yield f"{secret}."
        yield Usage(10, 1)

    run = api.post(f"/api/agents/{data_agent['id']}/runs").json()
    with use_model(model(script)):
        take_turn(api, data_agent["id"], run["id"], secret)
    [described] = run_tools(api, data_agent, ToolCall("describe_data", {}))
    rows = reported(api, data_agent)

    assert ("reporting", "usage") in {(t["schema"], t["name"]) for t in described["tables"]}
    assert any(r["agent_run_id"] == run["id"] for r in rows)
    assert secret not in json.dumps(rows)
    [view] = [t for t in described["tables"] if (t["schema"], t["name"]) == ("reporting", "usage")]
    texts = {c["name"] for c in view["columns"] if c["type"] == "text"}
    assert texts == {"kind", "purpose", "model", "cause"}


def test_embedding_batches_are_charged_with_their_purpose(api, user, data_agent, monkeypatch):  # noqa: F811
    monkeypatch.setattr(embeddings, "get_embedder", lambda: DeterministicFakeEmbedding(size=1024))
    store_id = store_of(user)
    # A search from the Search page: its query's embedding, charged to the person.
    resp = api.get("/api/search", params={"q": "pricing"})
    assert resp.status_code == 200, resp.text
    # The background sweep: chat messages' items, indexed.
    chat = make_chat(api, "Design")
    api.post(f"/api/chats/{chat['id']}/messages", json={"body": "the pricing page"})
    search_index.embed_pending(store_id)

    rows = [r for r in reported(api, data_agent) if r["kind"] == "embedding"]
    by_purpose = {r["purpose"]: r for r in rows}
    query = by_purpose["search_query"]
    assert (query["person_id"], query["quantity"], query["model"]) == (user.id, 1, EMBEDDER)
    # "pricing": seven characters, two tokens.
    assert query["input_tokens"] == 2
    assert query["cost"] == pytest.approx(2 / 1e6)
    indexed = by_purpose["index_embedding"]
    assert indexed["person_id"] is None and indexed["quantity"] >= 1


def test_usage_totals_are_admins_only(api, db, user):
    editor = join_organisation(db, user, "editor")
    with signed_in_as(editor):
        resp = api.get("/api/usage")
    assert resp.status_code == 403
    assert "usage" in resp.json()["detail"]


def test_a_range_picks_the_days_it_covers(api):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(model(_say("hi"))):
        take_turn(api, agent["id"], run["id"], "go")
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    assert agent["id"] in totals(api, start=today.isoformat(), end=today.isoformat())
    assert totals(api, start=yesterday.isoformat(), end=yesterday.isoformat()) == {}
    backwards = api.get("/api/usage", params={"start": today, "end": yesterday})
    assert backwards.status_code == 422


def test_a_workflow_runs_agent_nodes_are_charged_to_the_workflow_run(api, user, queued):  # noqa: F811
    a, b = make_agent(api, "First"), make_agent(api, "Second")
    flow = make_workflow(api, "Pipeline", [agent_node("a", a), agent_node("b", b)], [("a", "b")])
    with use_model(model(_say("ok"))):
        out = workflow_runs.run(api, queued, flow)
    assert out["status"] == "succeeded"

    mine = totals(api, "workflow")[flow["id"]]
    assert (mine["name"], mine["model_calls"]) == ("Pipeline", 2)
    assert totals(api)[a["id"]]["model_calls"] == 1
    assert totals(api, "person")[user.id]["model_calls"] == 2


def test_a_sandbox_sessions_seconds_are_charged_when_it_ends(api, user, backend):  # noqa: F811
    agent, run = python_agent(api)
    chat(api, agent, run, "print('hi')")
    assert totals(api)[agent["id"]]["sandbox_seconds"] == 0
    # Deleting the run ends its session.
    api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}")

    mine = totals(api)[agent["id"]]
    assert mine["sandbox_seconds"] > 0
    assert totals(api, "person")[user.id]["sandbox_seconds"] == mine["sandbox_seconds"]
    # At £0.001 a second, on top of the turn's model calls.
    calls = mine["input_tokens"] * 2 / 1e6 + mine["output_tokens"] * 10 / 1e6
    assert mine["cost"] == pytest.approx(calls + mine["sandbox_seconds"] * 0.001)
