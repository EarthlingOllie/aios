import asyncio
import threading
import time
from datetime import timedelta

import pytest
import test_agents
from conftest import delete_user, sign_up, store_of
from fake_model import ToolCall, last_tool_results, model, replies, use_model
from sqlalchemy import delete, func, update
from test_agents import agent_body, chat_body, make_skill, spy_run
from test_workflows import (
    agent_node,
    count_queries,
    make_agent,
    make_workflow,
    workflow_body,
    workflow_node,
)

from aios import checkpoints, workflows
from aios.api import workflows as workflows_api
from aios.auth import current_user
from aios.config import get_settings
from aios.db import SessionLocal
from aios.main import app
from aios.models import AgentRun, WorkflowRun, WorkflowRunStatus, WorkflowRunStep
from aios.tools import TOOLS, hybrid_search
from aios.workflows import execute_run, fail_run


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    """The ids of the runs the API starts, instead of sending them to the worker."""
    ids = []
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", ids.append)
    return ids


def echo(fail: set[str] = frozenset(), together: set[str] = frozenset()):
    """A model whose agents answer `<base prompt>(<message>)`, so outputs show what ran on what.

    Agents whose base prompt is in `fail` raise instead. Those in `together` each wait for all
    the others in it to be called too, so they only finish if they run at the same time.
    """
    arrived: set[str] = set()

    async def script(messages, info):
        name, text = messages[0].content, messages[-1].content
        if name in fail:
            raise RuntimeError(f"{name} broke")
        if name in together:
            arrived.add(name)
            for _ in range(500):
                if arrived >= together:
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError(f"{name} ran alone")
        yield f"{name}({text})"

    return model(script)


def agents(api, *names: str) -> dict[str, dict]:
    """Agents whose base prompt is their name (see `echo`)."""
    return {name: make_agent(api, name, base_prompt=name) for name in names}


def start(api, workflow: dict, text: str = "x") -> dict:
    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": text})
    assert resp.status_code == 201, resp.text
    return resp.json()


def run(api, queued: list[int], workflow: dict, text: str = "x", **script) -> dict:
    """Start a run, run it as the worker would, and return it as the API shows it."""
    started = start(api, workflow, text)
    assert queued[-1] == started["id"]
    with use_model(echo(**script)):
        asyncio.run(execute_run(started["id"]))
    resp = api.get(f"/api/workflows/{workflow['id']}/runs/{started['id']}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def steps(out: dict) -> dict[str, dict]:
    return {"/".join(s["path"]): s for s in out["steps"]}


def test_a_chain_passes_each_output_to_the_next_node(api, queued):
    a = agents(api, "first", "second")
    wf = make_workflow(
        api,
        "chain",
        [agent_node("one", a["first"]), agent_node("two", a["second"])],
        [("one", "two")],
    )
    started = start(api, wf, "hello")
    assert started["status"] == "queued"
    assert (started["input"], started["output"], started["started_at"]) == ("hello", None, None)
    assert (started["workflow_version"], started["workflow_version_id"]) == (
        1,
        wf["current_version"]["id"],
    )

    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"], out["error"]) == (
        "succeeded",
        "second(first(hello))",
        None,
    )
    assert out["started_at"] and out["finished_at"]
    assert [(s["path"], s["kind"], s["status"], s["input"], s["output"]) for s in out["steps"]] == [
        (["one"], "agent", "succeeded", "hello", "first(hello)"),
        (["two"], "agent", "succeeded", "first(hello)", "second(first(hello))"),
    ]

    # Each agent node's conversation is an agent run, pinned to the node's version, which
    # opens on the agent's run page like any other.
    one = out["steps"][0]
    assert one["agent_id"] == a["first"]["id"]
    agent_run = api.get(f"/api/agents/{one['agent_id']}/runs/{one['agent_run_id']}").json()
    assert (agent_run["title"], agent_run["message_count"], agent_run["agent_version"]) == (
        "hello",
        2,
        1,
    )
    assert [m["type"] for m in agent_run["messages"]] == ["human", "ai"]

    # Running it again doesn't start the run twice (e.g. a redelivered task).
    with use_model(echo(fail={"first"})):
        asyncio.run(execute_run(started["id"]))
    again = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert again["status"] == "succeeded" and len(again["steps"]) == 2


def test_fan_out_runs_branches_together_and_fan_in_joins_them_in_edge_order(api, queued):
    a = agents(api, "split", "left", "right", "join")
    nodes = [agent_node(k, a[k]) for k in ("split", "left", "right", "join")]
    edges = [("split", "left"), ("split", "right"), ("right", "join"), ("left", "join")]
    wf = make_workflow(api, "diamond", nodes, edges)

    out = run(api, queued, wf, together={"left", "right"})
    assert out["status"] == "succeeded", out["error"]
    by_path = steps(out)
    # Joined in edge order (right's edge comes first), not node order.
    assert by_path["join"]["input"] == "right(split(x))\n\nleft(split(x))"
    assert out["output"] == "join(right(split(x))\n\nleft(split(x)))"
    assert [s["path"][0] for s in out["steps"]][0] == "split"
    assert [s["path"][0] for s in out["steps"]][-1] == "join"


def test_start_nodes_get_the_input_and_sinks_make_the_output(api, queued):
    a = agents(api, "p", "q")
    wf = make_workflow(api, "pair", [agent_node("p", a["p"]), agent_node("q", a["q"])])
    out = run(api, queued, wf, "in", together={"p", "q"})
    # Both start nodes, both sinks: outputs in node order.
    assert out["output"] == "p(in)\n\nq(in)"


def test_a_workflow_node_runs_its_version_as_a_subgraph(api, queued):
    a = agents(api, "draft", "critic", "editor", "publish")
    inner = make_workflow(
        api,
        "review",
        [agent_node("critic", a["critic"]), agent_node("editor", a["editor"])],
        [("critic", "editor")],
    )
    outer = make_workflow(
        api,
        "pipeline",
        [
            agent_node("draft", a["draft"]),
            workflow_node("review", inner),
            agent_node("publish", a["publish"]),
        ],
        [("draft", "review"), ("review", "publish")],
    )
    # The node stays on the version it pins.
    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("review", []))

    out = run(api, queued, outer)
    assert out["status"] == "succeeded", out["error"]
    assert [(s["path"], s["kind"]) for s in out["steps"]] == [
        (["draft"], "agent"),
        (["review"], "workflow"),
        (["review", "critic"], "agent"),
        (["review", "editor"], "agent"),
        (["publish"], "agent"),
    ]
    by_path = steps(out)
    review = by_path["review"]
    assert (review["input"], review["output"]) == ("draft(x)", "editor(critic(draft(x)))")
    assert (review["agent_id"], review["agent_run_id"]) == (None, None)
    assert by_path["review/critic"]["input"] == "draft(x)"
    assert out["output"] == "publish(editor(critic(draft(x))))"


def test_a_failing_node_fails_the_run_and_nothing_downstream_starts(api, queued):
    a = agents(api, "start", "boom", "after", "side")
    inner = make_workflow(
        api,
        "inner",
        [agent_node("boom", a["boom"]), agent_node("after", a["after"])],
        [("boom", "after")],
    )
    wf = make_workflow(
        api,
        "fragile",
        [
            agent_node("start", a["start"]),
            workflow_node("sub", inner),
            agent_node("side", a["side"]),
            agent_node("end", a["after"]),
        ],
        [("start", "sub"), ("start", "side"), ("sub", "end")],
    )

    out = run(api, queued, wf, fail={"boom"})
    assert (out["status"], out["output"]) == ("failed", None)
    assert "boom broke" in out["error"]
    assert out["finished_at"]
    by_path = steps(out)
    assert by_path["sub/boom"]["status"] == "failed"
    assert by_path["sub/boom"]["error"] == "boom broke"
    assert by_path["sub"]["status"] == "failed"
    # The branch that was already running finished; nothing after the failure started.
    assert by_path["side"]["status"] == "succeeded"
    assert set(by_path) == {"start", "sub", "sub/boom", "side"}


def test_a_subworkflow_stopped_by_another_branchs_failure_fails_cleanly(api, queued):
    a = agents(api, "boom", "slow", "after")
    inner = make_workflow(
        api,
        "inner",
        [agent_node("slow", a["slow"]), agent_node("after", a["after"])],
        [("slow", "after")],
    )
    wf = make_workflow(api, "racing", [agent_node("boom", a["boom"]), workflow_node("sub", inner)])
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"

    async def script(messages, info):
        name = messages[0].content
        if name == "boom":
            raise RuntimeError("boom broke")
        if name == "slow":
            # Finishes only once boom's failure has been recorded, so `after` never starts.
            for _ in range(500):
                out = await asyncio.to_thread(lambda: api.get(url).json())
                if steps(out).get("boom", {}).get("status") == "failed":
                    break
                await asyncio.sleep(0.01)
        yield f"{name}({messages[-1].content})"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    out = api.get(url).json()
    assert (out["status"], out["error"]) == ("failed", "boom: boom broke")
    by_path = steps(out)
    assert by_path["sub/slow"]["status"] == "succeeded"
    assert (by_path["sub"]["status"], by_path["sub"]["error"]) == (
        "failed",
        "Stopped: another node failed",
    )
    assert set(by_path) == {"boom", "sub", "sub/slow"}


def test_a_run_stopped_midway_fails_the_steps_still_running(api, queued):
    a = agents(api, "quick", "slow")
    inner = make_workflow(api, "inner", [agent_node("slow", a["slow"])])
    wf = make_workflow(
        api, "stalls", [agent_node("quick", a["quick"]), workflow_node("sub", inner)]
    )
    started = start(api, wf)

    async def script(messages, info):
        if messages[0].content == "slow":
            await asyncio.sleep(60)
        yield "done"

    async def stopped():
        # As the worker's time limit stops it.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(execute_run(started["id"]), 5)

    with use_model(model(script)):
        asyncio.run(stopped())
    fail_run(started["id"], "Too slow")
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["error"]) == ("failed", "Too slow")
    by_path = steps(out)
    assert by_path["quick"]["status"] == "succeeded"
    for key in ("sub", "sub/slow"):
        assert by_path[key]["status"] == "failed"
        assert by_path[key]["error"] == "The run stopped before this step finished."
        assert by_path[key]["finished_at"]


def test_a_run_that_cant_load_fails_with_a_clear_error(api, queued, monkeypatch):
    searcher = make_agent(api, "searcher")
    searcher = api.put(
        f"/api/agents/{searcher['id']}",
        json={"name": "searcher", "tools": [{"key": "web_search", "config": {}}]},
    ).json()
    wf = make_workflow(api, "search", [agent_node("s", searcher)])
    started = start(api, wf)
    # The tool leaves the library between starting the run and the worker picking it up.
    monkeypatch.delitem(TOOLS, "web_search")
    asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "failed"
    assert "Unknown tool: web_search" in out["error"]
    assert out["steps"] == []


def test_a_run_whose_data_store_cant_be_resolved_fails_with_a_clear_error(api, queued, monkeypatch):
    wf = make_workflow(api, "w", [agent_node("a", make_agent(api, "a"))])
    started = start(api, wf)

    def broken(*args):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(workflows, "data_store_of", broken)
    # Failed then and there, not left running without a worker until it's failed as stopped.
    asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "failed"
    assert out["error"] == "store unavailable"
    assert out["finished_at"]
    assert out["steps"] == []


def test_an_agent_nodes_trace_has_its_agents_metadata_and_the_workflows(api, queued, monkeypatch):
    poet = test_agents.make_agent(api, "Poet", "Write.", [make_skill(api, "Haiku", "5-7-5")])
    url = f"/api/agents/{poet['id']}"
    tools = [{"key": "web_search", "config": {}}]
    poet = api.put(url, json=agent_body(poet, tools=tools)).json()
    wf = make_workflow(api, "poems", [agent_node("p", poet)])
    traced = []
    trace = workflows.trace_metadata
    monkeypatch.setattr(
        workflows, "trace_metadata", lambda *a: traced.append(trace(*a)) or traced[-1]
    )
    out = run(api, queued, wf)
    (node,) = traced

    # The same as a chat turn of the agent's version, so filters on e.g. tools or skills find
    # both, plus where in which workflow run it ran.
    seen = spy_run(monkeypatch)
    chat = api.post(f"{url}/runs").json()
    with use_model(replies("ok")):
        assert api.post(f"{url}/runs/{chat['id']}/chat", json=chat_body("hi")).status_code == 200
    agent_run_id = out["steps"][0]["agent_run_id"]
    assert node == seen["metadata"] | {
        "langfuse_session_id": checkpoints.thread_id(agent_run_id),
        "langfuse_tags": [*seen["metadata"]["langfuse_tags"], "workflow:poems"],
        "agent_run_id": agent_run_id,
        "workflow_id": wf["id"],
        "workflow_name": "poems",
        "workflow_version": 1,
        "workflow_run_id": out["id"],
        "workflow_path": ["p"],
    }
    assert node["skill_versions"][0]["name"] == "Haiku"
    assert node["tools"][0]["key"] == "web_search"


def test_an_agent_nodes_tools_search_the_owners_data_store(api, user, queued, monkeypatch):
    searched = []

    async def search(**kwargs):
        searched.append(kwargs["data_store_id"])
        return []

    async def script(messages, info):
        if last_tool_results(messages):
            yield "done"
        else:
            yield ToolCall("search_documents", {"query": "fish"})

    monkeypatch.setattr(hybrid_search, "document_search_results", search)
    librarian = make_agent(api, "Librarian")
    tools = [{"key": "hybrid_search", "config": {}}]
    url = f"/api/agents/{librarian['id']}"
    librarian = api.put(url, json=agent_body(librarian, tools=tools)).json()
    wf = make_workflow(api, "lookup", [agent_node("l", librarian)])
    started = start(api, wf)
    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))

    assert searched == [store_of(user)]


def test_get_shows_the_pinned_version_and_runs_list_newest_first(api, queued):
    a = agents(api, "solo", "other")
    wf = make_workflow(api, "flow", [agent_node("solo", a["solo"])])
    first = start(api, wf, "one")
    api.put(
        f"/api/workflows/{wf['id']}", json=workflow_body("flow", [agent_node("other", a["other"])])
    )
    second = start(api, wf, "two")

    listed = api.get(f"/api/workflows/{wf['id']}/runs").json()
    assert [(r["id"], r["workflow_version"]) for r in listed] == [
        (second["id"], 2),
        (first["id"], 1),
    ]
    assert listed[0]["status"] == "queued"

    out = api.get(f"/api/workflows/{wf['id']}/runs/{first['id']}").json()
    version = api.get(f"/api/workflows/{wf['id']}/versions/1").json()
    assert out["version"] == version
    assert [n["key"] for n in out["version"]["nodes"]] == ["solo"]
    assert out["steps"] == []
    assert {k: out[k] for k in first} == first


def test_runs_belong_to_the_workflows_owner(api, client, db, queued):
    a = agents(api, "mine")
    wf = make_workflow(api, "mine", [agent_node("mine", a["mine"])])
    other_wf = make_workflow(api, "also mine", [agent_node("mine", a["mine"])])
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs"

    # Not under another of the user's workflows.
    assert api.get(f"/api/workflows/{other_wf['id']}/runs/{started['id']}").status_code == 404
    assert api.delete(f"/api/workflows/{other_wf['id']}/runs/{started['id']}").status_code == 404
    assert api.get(f"{url}/{2**31}").status_code == 422

    other = sign_up(db, -2, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(url).status_code == 404
        assert client.post(url, json={"input": "x"}).status_code == 404
        assert client.get(f"{url}/{started['id']}").status_code == 404
        assert client.delete(f"{url}/{started['id']}").status_code == 404
    finally:
        delete_user(db, other)
    assert queued == [started["id"]]


def test_deleting_a_run_deletes_its_agent_runs(api, db, queued, monkeypatch):
    a = agents(api, "one", "two")
    wf = make_workflow(
        api, "pair", [agent_node("one", a["one"]), agent_node("two", a["two"])], [("one", "two")]
    )
    out = run(api, queued, wf)
    agent_run_ids = [s["agent_run_id"] for s in out["steps"]]
    assert all(agent_run_ids) and checkpoints.history(agent_run_ids[0])

    url = f"/api/workflows/{wf['id']}/runs/{out['id']}"
    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []
    db.expire_all()
    assert all(db.get(AgentRun, i) is None for i in agent_run_ids)
    assert checkpoints.history(agent_run_ids[0]) == []
    assert api.get(f"/api/agents/{a['one']['id']}/runs").json() == []


def test_deleting_a_workflow_or_agent_leaves_the_runs(api, db, queued):
    a = agents(api, "gone")
    wf = make_workflow(api, "flow", [agent_node("gone", a["gone"])])
    out = run(api, queued, wf)

    # Soft-deleting the workflow keeps its runs; its agent can then go, taking its agent runs
    # with it, and the step stays without one.
    assert api.delete(f"/api/workflows/{wf['id']}").status_code == 204
    assert api.delete(f"/api/agents/{a['gone']['id']}").status_code == 204
    db.expire_all()
    run_row = db.get(WorkflowRun, out["id"])
    assert run_row is not None
    step = db.get(WorkflowRunStep, out["steps"][0]["id"])
    assert (step.output, step.agent_run_id) == ("gone(x)", None)
    # The workflow itself is gone from the API.
    assert api.get(f"/api/workflows/{wf['id']}/runs").status_code == 404


def test_a_workflow_that_cant_run_isnt_started(api, queued, monkeypatch):
    empty = make_workflow(api, "empty")
    resp = api.post(f"/api/workflows/{empty['id']}/runs", json={"input": "x"})
    assert resp.status_code == 422

    searcher = make_agent(api, "searcher")
    searcher = api.put(
        f"/api/agents/{searcher['id']}",
        json={"name": "searcher", "tools": [{"key": "web_search", "config": {}}]},
    ).json()
    inner = make_workflow(api, "inner", [agent_node("s", searcher)])
    middle = make_workflow(api, "middle", [workflow_node("inner", inner)])
    outer = make_workflow(api, "outer", [workflow_node("middle", middle)])
    monkeypatch.delitem(TOOLS, "web_search")

    # However deep the agent version that can't be built.
    resp = api.post(f"/api/workflows/{outer['id']}/runs", json={"input": "x"})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "Agent searcher v2 can't run: Unknown tool: web_search",
    )
    assert api.get(f"/api/workflows/{outer['id']}/runs").json() == []
    assert queued == []


def test_starting_a_run_takes_a_fixed_number_of_queries(api, queued):
    """Checking every version a run will run doesn't cost queries per node, agent or depth."""
    searchers = []
    for i in range(6):
        searcher = make_agent(api, f"searcher {i}")
        tools = [{"key": "web_search", "config": {}}]
        body = {"name": f"searcher {i}", "tools": tools}
        searchers.append(api.put(f"/api/agents/{searcher['id']}", json=body).json())

    def start_queries(n: int) -> int:
        # n agents side by side, under n workflows nested one in the next.
        workflow = make_workflow(
            api, f"inner {n}", [agent_node(f"a{i}", a) for i, a in enumerate(searchers[:n])]
        )
        for depth in range(n):
            workflow = make_workflow(api, f"wf {n} {depth}", [workflow_node("w", workflow)])
        with count_queries() as statements:
            start(api, workflow)
        return len(statements)

    assert start_queries(1) == start_queries(6)


def test_a_run_that_cant_be_queued_isnt_left_behind(api, monkeypatch):
    a = agents(api, "solo")
    wf = make_workflow(api, "flow", [agent_node("solo", a["solo"])])

    def broker_down(run_id):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", broker_down)
    resp = api.post(f"/api/workflows/{wf['id']}/runs", json={"input": "x"})
    assert resp.status_code == 503
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []


def test_an_agent_deleted_since_a_subworkflow_version_pinned_it_cant_run(api, queued):
    a = agents(api, "retired")
    inner = make_workflow(api, "inner", [agent_node("old", a["retired"])])
    outer = make_workflow(api, "outer", [workflow_node("inner", inner)])
    # Outer keeps inner v1, which runs the agent: it can only be deleted with outer deactivated.
    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("inner", []))
    url = f"/api/agents/{a['retired']['id']}"
    assert api.delete(url).status_code == 409
    resolved = {"deactivate_workflows": [outer["id"]]}
    assert api.request("DELETE", url, json=resolved).status_code == 204

    resp = api.post(f"/api/workflows/{outer['id']}/runs", json={"input": "x"})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "Agent retired v1 can't run: it has been deleted",
    )
    assert queued == []


def test_progress_is_visible_while_the_run_goes(api, queued):
    a = agents(api, "first", "second")
    wf = make_workflow(
        api,
        "chain",
        [agent_node("one", a["first"]), agent_node("two", a["second"])],
        [("one", "two")],
    )
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    seen = []

    async def script(messages, info):
        if messages[0].content == "second":
            # Mid-run, as the UI polls it.
            seen.append(await asyncio.to_thread(lambda: api.get(url).json()))
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    (during,) = seen
    assert (during["status"], during["started_at"] is not None) == ("running", True)
    assert [(s["path"], s["status"], s["output"]) for s in during["steps"]] == [
        (["one"], "succeeded", "done"),
        (["two"], "running", None),
    ]
    # The conversation can be opened while it's going.
    assert during["steps"][1]["agent_run_id"] is not None
    assert api.get(url).json()["status"] == "succeeded"


def test_a_run_that_takes_too_long_is_stopped(api, queued, monkeypatch):
    monkeypatch.setattr(get_settings(), "workflow_run_time_limit", 0.5)
    a = agents(api, "slow", "after")
    wf = make_workflow(
        api,
        "sluggish",
        [agent_node("slow", a["slow"]), agent_node("after", a["after"])],
        [("slow", "after")],
    )
    started = start(api, wf)

    async def script(messages, info):
        await asyncio.sleep(60)
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("failed", None)
    assert out["error"].startswith("The run took longer than")
    assert [s["path"] for s in out["steps"]] == [["slow"]]


def test_a_run_whose_worker_died_fails_when_its_task_is_redelivered(api, queued, monkeypatch):
    a = agents(api, "first", "second")
    wf = make_workflow(
        api,
        "chain",
        [agent_node("one", a["first"]), agent_node("two", a["second"])],
        [("one", "two")],
    )
    started = start(api, wf)

    class WorkerDied(BaseException):
        """Stands in for the worker being killed: nothing gets to record what happened."""

    dying = []
    finish_step = workflows._finish_step

    def finish_or_die(*args) -> None:
        if dying:
            raise WorkerDied
        finish_step(*args)

    async def script(messages, info):
        if messages[0].content == "second":
            dying.append(True)
        yield "done"

    # Killed while node two is running.
    monkeypatch.setattr(workflows, "_finish_step", finish_or_die)
    with use_model(model(script)), pytest.raises(WorkerDied):
        asyncio.run(execute_run(started["id"]))
    dying.clear()
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    assert api.get(url).json()["status"] == "running"

    # The message wasn't acked, so the task comes round again. Straight away (the worker was
    # lost), its heartbeat is still fresh: it could be a live run's, so it's left alone.
    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))
    assert run_status(started["id"]) == WorkflowRunStatus.running

    # Once the heartbeat has stopped, the run can't be going: rather than leave it running
    # forever, it fails, and the step that was cut off.
    age_heartbeat(started["id"])
    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))
    assert run_status(started["id"]) == WorkflowRunStatus.failed
    out = api.get(url).json()
    assert (out["status"], out["output"], out["error"]) == (
        "failed",
        None,
        workflows.WORKER_STOPPED,
    )
    assert out["finished_at"]
    assert [(s["path"], s["status"]) for s in out["steps"]] == [
        (["one"], "succeeded"),
        (["two"], "failed"),
    ]
    assert out["steps"][1]["error"] == "The run stopped before this step finished."
    assert out["steps"][1]["finished_at"]


def test_a_live_run_isnt_failed_when_its_task_is_redelivered(api, queued):
    # Its message can come round again while it's going, e.g. once the broker's visibility
    # timeout, shorter than a run can take, has passed.
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])
    started = start(api, wf)
    seen = []

    async def script(messages, info):
        await asyncio.to_thread(execute_redelivered, started["id"])
        seen.append(run_status(started["id"]))
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    assert seen == [WorkflowRunStatus.running]
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("succeeded", "done")


def test_a_run_whose_worker_stopped_is_shown_failed(api, queued):
    # A lost worker's task is redelivered straight away, while its heartbeat is still fresh, so
    # it's left running; reading it once the heartbeat has stopped fails it instead.
    a = agents(api, "solo")
    wf = make_workflow(api, "flow", [agent_node("solo", a["solo"])])
    listed, got = start(api, wf), start(api, wf)
    for run_id in (listed["id"], got["id"]):
        abandon(run_id)
    url = f"/api/workflows/{wf['id']}/runs"

    by_id = {r["id"]: r for r in api.get(url).json()}
    assert (by_id[listed["id"]]["status"], by_id[listed["id"]]["error"]) == (
        "failed",
        workflows.WORKER_STOPPED,
    )
    assert by_id[listed["id"]]["finished_at"]
    out = api.get(f"{url}/{got['id']}").json()
    assert (out["status"], out["error"]) == ("failed", workflows.WORKER_STOPPED)
    assert [(s["status"], s["error"]) for s in out["steps"]] == [
        ("failed", "The run stopped before this step finished.")
    ]


def test_a_run_refreshes_its_heartbeat_while_it_goes(api, queued, monkeypatch):
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.05)
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])
    started = start(api, wf)
    beats = []

    async def script(messages, info):
        for _ in range(2):
            beats.append(await asyncio.to_thread(heartbeat, started["id"]))
            await asyncio.sleep(0.5)
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    first, second = beats
    assert first is not None and second > first


def test_a_heartbeat_that_cant_be_written_doesnt_stop_the_run(api, queued, monkeypatch, caplog):
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.05)

    def broken(run_id: int) -> None:
        raise ConnectionError("db is down")

    monkeypatch.setattr(workflows, "_beat", broken)
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])

    async def script(messages, info):
        await asyncio.sleep(0.3)
        yield "done"

    started = start(api, wf)
    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "succeeded"
    # Logged, not fatal: a run outlives a blip, and fails only once beats have been missed long
    # enough (`aios.workflows.STALE_AFTER`).
    assert any("heartbeat" in r.getMessage() for r in caplog.records)


def test_a_run_keeps_its_heartbeat_while_its_event_loop_is_busy(api, queued, monkeypatch):
    # A node can hold the worker's event loop for longer than a run takes to go stale (a long
    # blocking call, CPU-bound work); the worker is still alive and will finish it.
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr(workflows, "STALE_AFTER", timedelta(seconds=0.5))
    a = agents(api, "busy")
    wf = make_workflow(api, "flow", [agent_node("busy", a["busy"])])
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    seen = []

    def read_later():
        time.sleep(1.0)
        # Reading it fails it if its heartbeat has gone stale (`fail_stopped_runs`).
        seen.append(api.get(url).json()["status"])

    async def script(messages, info):
        reader = threading.Thread(target=read_later)
        reader.start()
        time.sleep(1.5)  # Blocks the event loop, three times as long as the run's lease.
        reader.join()
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    assert seen == ["running"]
    out = api.get(url).json()
    assert (out["status"], out["output"], out["error"]) == ("succeeded", "done", None)


def run_status(run_id: int) -> WorkflowRunStatus:
    """As stored, without reading it through the API (which fails a run whose worker stopped)."""
    with SessionLocal() as db:
        return db.get(WorkflowRun, run_id).status


def heartbeat(run_id: int):
    with SessionLocal() as db:
        return db.get(WorkflowRun, run_id).heartbeat_at


def age_heartbeat(run_id: int) -> None:
    """As if its worker had stopped beating a day ago."""
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(heartbeat_at=func.now() - timedelta(days=1))
        )
        db.commit()


def abandon(run_id: int) -> None:
    """A queued run as a worker that died a day into it, mid-node, leaves it."""
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(status=WorkflowRunStatus.running, started_at=func.now() - timedelta(days=1))
        )
        db.add(WorkflowRunStep(run_id=run_id, path=["solo"], kind="agent", input="x"))
        db.commit()
    age_heartbeat(run_id)


def execute_redelivered(run_id: int) -> None:
    """The run's task, delivered again to another worker."""
    asyncio.run(execute_run(run_id))


def delete_run(run_id: int) -> None:
    with SessionLocal() as db:
        db.execute(delete(WorkflowRun).where(WorkflowRun.id == run_id))
        db.commit()


def test_a_run_deleted_while_it_goes_starts_nothing_more(api, queued, caplog, monkeypatch):
    a = agents(api, "start", "left", "right", "after")
    wf = make_workflow(
        api,
        "doomed",
        [
            agent_node("start", a["start"]),
            agent_node("left", a["left"]),
            agent_node("right", a["right"]),
            agent_node("left2", a["after"]),
            agent_node("right2", a["after"]),
        ],
        [("start", "left"), ("start", "right"), ("left", "left2"), ("right", "right2")],
    )
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    tried = []
    start_step = workflows._start_step

    def trying(run_id, path, node, text):
        tried.append("/".join(path))
        return start_step(run_id, path, node, text)

    monkeypatch.setattr(workflows, "_start_step", trying)

    async def script(messages, info):
        name = messages[0].content
        if name == "left":
            # As deleting it does, less its agent runs: the API's delete would wait for this
            # turn to end before it returned (`checkpoints.delete_threads`).
            await asyncio.to_thread(delete_run, started["id"])
        if name == "right":
            # Still going once the other branch has found the run gone.
            await asyncio.sleep(0.5)
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    # The other branch's next node isn't tried once the first found the run gone.
    # (left and right start together, so in either order.)
    assert tried[0] == "start" and sorted(tried[1:3]) == ["left", "right"]
    assert tried[3:] == ["left2"]
    assert api.get(url).status_code == 404
    # Not logged as a failure: the user deleted it.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_a_running_run_cant_be_deleted_until_it_finishes(api, db, queued):
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])
    started = start(api, wf)
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    refused = []

    async def script(messages, info):
        # Its agent turns would carry on without it.
        refused.append(await asyncio.to_thread(lambda: api.delete(url)))
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    (resp,) = refused
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "This run is still going; wait for it to finish.",
    )
    assert api.get(url).json()["status"] == "succeeded"
    assert api.delete(url).status_code == 204

    # However long a run has been going, it can't go while its worker is beating; once that
    # has stopped (the worker died), it can.
    stuck = start(api, wf)
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == stuck["id"])
        .values(
            status=WorkflowRunStatus.running,
            started_at=func.now() - timedelta(days=1),
            heartbeat_at=func.now(),
        )
    )
    db.commit()
    stuck_url = f"/api/workflows/{wf['id']}/runs/{stuck['id']}"
    assert api.delete(stuck_url).status_code == 409
    age_heartbeat(stuck["id"])
    assert api.delete(stuck_url).status_code == 204


def test_a_queued_run_can_be_deleted_and_then_never_starts(api, queued):
    a = agents(api, "solo")
    wf = make_workflow(api, "flow", [agent_node("solo", a["solo"])])
    started = start(api, wf)
    assert api.delete(f"/api/workflows/{wf['id']}/runs/{started['id']}").status_code == 204
    with use_model(echo()):
        asyncio.run(execute_run(started["id"]))
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []


def test_runs_go_to_their_own_queue():
    # So a run can't hold up ingests: its own worker takes it, and the redelivery timeout stays
    # ingest's. A run needn't fit in it: a redelivered run doesn't start again, and is failed
    # only once its heartbeat has stopped.
    from aios.config import get_settings
    from aios.worker import celery_app

    def queue(task: str) -> str:
        return celery_app.amqp.router.route({}, task)["queue"].name

    assert queue("aios.run_workflow") == "workflows"
    assert queue("aios.ingest_document") == "celery"
    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert timeout < get_settings().workflow_run_time_limit


def test_a_run_ended_elsewhere_stops_its_worker(api, queued, monkeypatch):
    """A run failed while it goes (its read found the worker stale) or deleted stops at its next
    heartbeat, rather than spending turns nobody will see."""
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.05)
    a = agents(api, "slow", "after")
    wf = make_workflow(
        api,
        "flow",
        [agent_node("slow", a["slow"]), agent_node("after", a["after"])],
        [("slow", "after")],
    )
    started = start(api, wf)

    async def script(messages, info):
        if messages[0].content == "slow":
            await asyncio.to_thread(fail_run, started["id"], "Stopped elsewhere")
            await asyncio.sleep(30)
        yield "done"

    async def timed():
        async with asyncio.timeout(5):
            await execute_run(started["id"])

    with use_model(model(script)):
        asyncio.run(timed())
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["error"]) == ("failed", "Stopped elsewhere")
    assert [s["path"] for s in out["steps"]] == [["slow"]]
    assert out["steps"][0]["status"] == "failed"


def test_a_run_whose_event_loop_makes_no_progress_is_failed_and_stopped(api, queued, monkeypatch):
    # A worker whose event loop is stuck still beats (from its own thread), so it would stay
    # running until Celery's hard time limit; once the loop has made no progress for long
    # enough, the run fails instead, and stops as soon as the loop gets going again.
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(get_settings(), "workflow_run_no_progress_limit", 0.3)
    a = agents(api, "stuck", "after")
    wf = make_workflow(
        api,
        "flow",
        [agent_node("stuck", a["stuck"]), agent_node("after", a["after"])],
        [("stuck", "after")],
    )
    started = start(api, wf)
    seen = []

    def read_later():
        time.sleep(1.0)
        with SessionLocal() as db:
            run = db.get(WorkflowRun, started["id"])
            seen.append((run.status, run.error))

    async def script(messages, info):
        if messages[0].content == "stuck":
            reader = threading.Thread(target=read_later)
            reader.start()
            time.sleep(1.5)  # Holds the event loop, five times the limit.
            reader.join()
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    # Failed while the loop was still stuck, with an error saying why.
    assert seen == [(WorkflowRunStatus.failed, workflows.no_progress_error())]
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"], out["error"]) == (
        "failed",
        None,
        workflows.no_progress_error(),
    )
    # Stopped once the loop was free: nothing more started, and the node it was stuck in stays
    # failed, though it went on to finish.
    assert [(s["path"], s["status"], s["error"]) for s in out["steps"]] == [
        (["stuck"], "failed", "The run stopped before this step finished.")
    ]


def test_a_run_awaiting_a_slow_node_isnt_failed_for_making_no_progress(api, queued, monkeypatch):
    # A node waiting on a long model or tool call leaves the loop free to make progress. The
    # limit is 1s and the wait 3s: a wait well past the limit, with room for a loaded machine
    # to stall a beat now and then (0.3s wasn't, on CI).
    monkeypatch.setattr(workflows, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(get_settings(), "workflow_run_no_progress_limit", 1.0)
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])
    started = start(api, wf)

    async def script(messages, info):
        await asyncio.sleep(3.0)
        yield "done"

    with use_model(model(script)):
        asyncio.run(execute_run(started["id"]))
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"], out["error"]) == ("succeeded", "done", None)


def test_the_no_progress_limit_outlasts_a_dead_workers_lease():
    # A loop held for a while (a blocking call, CPU-bound work) isn't stuck: only one held well
    # past the time a dead worker's run is failed in is.
    limit = get_settings().workflow_run_no_progress_limit
    assert (
        2 * workflows.STALE_AFTER.total_seconds() < limit < get_settings().workflow_run_time_limit
    )
