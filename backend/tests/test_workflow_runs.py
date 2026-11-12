import asyncio
import uuid
from datetime import timedelta

import pytest
import test_agents
from conftest import (
    QueuedRuns,
    capture_runs,
    delete_user,
    drain_run,
    enqueue_advance,
    org_of,
    provisioned_user,
    run_workflow,
    store_of,
)
from sqlalchemy import delete, func, select, update
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
from aios.datastores import store_session
from aios.datastores.models import AgentRun, Turn, WorkflowRun
from aios.db import SessionLocal
from aios.main import app
from aios.models import WorkItem, WorkKind
from aios.run_service import STOPPED
from aios.testing.fake_model import (
    ToolCall,
    instructions,
    last_tool_results,
    model,
    node_input,
    replies,
    use_model,
)
from aios.tools import TOOLS, hybrid_search
from aios.workflows import advance as advance_module
from aios.workflows import fail_run


@pytest.fixture
def queued(monkeypatch, user) -> QueuedRuns:
    """The runs the API starts, for the test to run (`drain_run`)."""
    return capture_runs(monkeypatch, store_of(user))


def echo(fail: set[str] = frozenset(), together: set[str] = frozenset()):
    """A model whose agents answer `<base prompt>(<message>)`, so outputs show what ran on what.

    Agents whose base prompt is in `fail` raise instead. Those in `together` each wait for all
    the others in it to be called too, so they only finish if they run at the same time.
    """
    arrived: set[str] = set()

    async def script(messages, info):
        name, text = instructions(messages), node_input(messages[-1])
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
    """Start a run, run it on the run service, and return it as the API shows it."""
    started = start(api, workflow, text)
    assert queued[-1] == started["id"]
    with use_model(echo(**script)):
        drain_run(queued.store, started["id"])
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
        drain_run(queued.store, started["id"])
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

    # Advancing it again (e.g. an advance enqueued twice) doesn't start the run twice.
    with use_model(echo(fail={"first"})):
        drain_run(queued.store, started["id"])
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
        name = instructions(messages)
        if name == "boom":
            raise RuntimeError("boom broke")
        if name == "slow":
            # Finishes only once boom's failure has been recorded, so `after` never starts.
            for _ in range(500):
                out = await asyncio.to_thread(lambda: api.get(url).json())
                if steps(out).get("boom", {}).get("status") == "failed":
                    break
                await asyncio.sleep(0.01)
        yield f"{name}({node_input(messages[-1])})"

    with use_model(model(script)):
        drain_run(queued.store, started["id"])
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
        if instructions(messages) == "slow":
            await asyncio.sleep(60)
        yield "done"

    async def stopped():
        # As the run service stopping part way through it.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_workflow(queued.store, started["id"]), 5)

    with use_model(model(script)):
        asyncio.run(stopped())
    fail_run(queued.store, started["id"], "Too slow")
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
    # The tool leaves the library between starting the run and its first advance.
    monkeypatch.delitem(TOOLS, "web_search")
    drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "failed"
    assert "Unknown tool: web_search" in out["error"]
    assert out["steps"] == []


def test_a_run_whose_data_store_cant_be_resolved_fails_with_a_clear_error(api, queued, monkeypatch):
    wf = make_workflow(api, "w", [agent_node("a", make_agent(api, "a"))])
    started = start(api, wf)

    def broken(*args):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(workflows, "_organisation", broken)
    # Failed then and there, not left running with nothing to advance it.
    drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "failed"
    assert out["error"] == "store unavailable"
    assert out["finished_at"]
    assert out["steps"] == []


def test_an_agent_nodes_trace_has_its_agents_metadata_and_the_workflows(
    api, db, user, queued, monkeypatch
):
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
        "langfuse_session_id": f"agent-run-{org_of(db, user).id}-{agent_run_id}",
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
        drain_run(queued.store, started["id"])

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

    # Another organisation, with a store of its own: it has no such workflow (ids are each
    # store's own), and the run isn't its.
    other = provisioned_user(db, "other")
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(url).status_code == 404
        assert client.post(url, json={"input": "x"}).status_code == 404
        assert client.get(f"{url}/{started['id']}").status_code == 404
        assert client.delete(f"{url}/{started['id']}").status_code == 404
    finally:
        delete_user(db, other)
    assert queued == [started["id"]]


def test_deleting_a_run_deletes_its_agent_runs(api, db, queued, monkeypatch, in_store):
    a = agents(api, "one", "two")
    wf = make_workflow(
        api, "pair", [agent_node("one", a["one"]), agent_node("two", a["two"])], [("one", "two")]
    )
    out = run(api, queued, wf)
    agent_run_ids = [s["agent_run_id"] for s in out["steps"]]
    assert all(agent_run_ids) and checkpoints.history(queued.store, agent_run_ids[0])

    url = f"/api/workflows/{wf['id']}/runs/{out['id']}"
    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []
    assert all(in_store.get(AgentRun, i) is None for i in agent_run_ids)
    assert checkpoints.history(queued.store, agent_run_ids[0]) == []
    assert api.get(f"/api/agents/{a['one']['id']}/runs").json() == []


def test_archiving_a_workflow_or_agent_leaves_the_runs(api, queued):
    a = agents(api, "retired")
    wf = make_workflow(api, "flow", [agent_node("retired", a["retired"])])
    out = run(api, queued, wf)

    # Archiving the workflow and its agent throws nothing away: the run, its step and the
    # step's agent run all still read.
    assert api.post(f"/api/workflows/{wf['id']}/archive").status_code == 200
    assert api.post(f"/api/agents/{a['retired']['id']}/archive").status_code == 200
    [listed] = api.get(f"/api/workflows/{wf['id']}/runs").json()
    assert listed["id"] == out["id"]
    [step] = api.get(f"/api/workflows/{wf['id']}/runs/{out['id']}").json()["steps"]
    assert step["output"] == "retired(x)"
    agent_run = api.get(f"/api/agents/{a['retired']['id']}/runs/{step['agent_run_id']}")
    assert agent_run.status_code == 200


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

    def queue_down(work):
        raise ConnectionError("the platform database is down")

    monkeypatch.setattr(workflows_api, "enqueue_run", queue_down)
    resp = api.post(f"/api/workflows/{wf['id']}/runs", json={"input": "x"})
    assert resp.status_code == 503
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []


def test_an_agent_archived_since_a_subworkflow_version_pinned_it_cant_run(api, queued):
    a = agents(api, "retired")
    inner = make_workflow(api, "inner", [agent_node("old", a["retired"])])
    outer = make_workflow(api, "outer", [workflow_node("inner", inner)])
    # Outer keeps inner v1, which runs the agent, though inner no longer does.
    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("inner", []))
    assert api.post(f"/api/agents/{a['retired']['id']}/archive").status_code == 200

    resp = api.post(f"/api/workflows/{outer['id']}/runs", json={"input": "x"})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "The agent retired is archived.",
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
        if instructions(messages) == "second":
            # Mid-run, as the UI polls it.
            seen.append(await asyncio.to_thread(lambda: api.get(url).json()))
        yield "done"

    with use_model(model(script)):
        drain_run(queued.store, started["id"])
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
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("failed", None)
    assert out["error"].startswith("The run took longer than")
    assert [s["path"] for s in out["steps"]] == [["slow"]]


class ProcessDied(BaseException):
    """Stands in for a run service process being killed: nothing gets to record what
    happened."""


def lapse_leases() -> None:
    """As if the processes holding the queue's items had died a while ago: their leases lapse."""
    with SessionLocal() as db:
        db.execute(
            update(WorkItem)
            .where(WorkItem.leased_until.is_not(None))
            .values(leased_until=func.now() - timedelta(seconds=1))
        )
        db.commit()


def turns_of(store_id: uuid.UUID, agent_run_id: int) -> list[tuple[str, str | None]]:
    with store_session(store_id) as store:
        return [
            (t.status.value, t.error)
            for t in store.scalars(
                select(Turn).where(Turn.agent_run_id == agent_run_id).order_by(Turn.id)
            )
        ]


def test_a_node_whose_process_died_is_carried_on_when_its_item_is_claimed_again(
    api, queued, monkeypatch
):
    a = agents(api, "first", "second")
    wf = make_workflow(
        api,
        "chain",
        [agent_node("one", a["first"]), agent_node("two", a["second"])],
        [("one", "two")],
    )
    started = start(api, wf)
    dying = []
    finish_step = workflows._finish_step

    def finish_or_die(*args) -> None:
        if dying:
            dying.clear()
            raise ProcessDied
        finish_step(*args)

    async def script(messages, info):
        if instructions(messages) == "second":
            dying.append(True)
        yield "done"

    # Killed while node two finishes: its turn has ended, but not its step.
    monkeypatch.setattr(workflows, "_finish_step", finish_or_die)
    with use_model(model(script)):
        # The item dies with its process: still leased, nothing recorded.
        drain_run(queued.store, started["id"])
    url = f"/api/workflows/{wf['id']}/runs/{started['id']}"
    assert api.get(url).json()["status"] == "running"

    # Its item is claimed again once its lease lapses, and the node carries on from its turn,
    # rather than asking the agent again.
    lapse_leases()
    with use_model(echo(fail={"second"})):
        drain_run(queued.store, started["id"])
    out = api.get(url).json()
    assert (out["status"], out["output"], out["error"]) == ("succeeded", "done", None)
    two = steps(out)["two"]
    assert turns_of(queued.store, two["agent_run_id"]) == [("succeeded", None)]
    conversation = api.get(f"/api/agents/{two['agent_id']}/runs/{two['agent_run_id']}").json()
    assert [m["type"] for m in conversation["messages"]] == ["human", "ai"]


def test_a_turn_cut_off_part_way_carries_on_from_its_last_checkpoint(api, queued):
    a = agents(api, "slow")
    wf = make_workflow(api, "flow", [agent_node("slow", a["slow"])])
    started = start(api, wf)

    async def slow(messages, info):
        await asyncio.sleep(60)
        yield "never"

    async def cut_off():
        # As a run service process stopped mid-turn, its item handed back.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_workflow(queued.store, started["id"]), 3)

    with use_model(model(slow)):
        asyncio.run(cut_off())
    lapse_leases()
    with use_model(echo()):
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"]) == ("succeeded", "slow(x)")
    step = out["steps"][0]
    # One turn, carried on: the message wasn't given twice.
    assert turns_of(queued.store, step["agent_run_id"]) == [("succeeded", None)]
    conversation = api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}").json()
    assert [m["type"] for m in conversation["messages"]] == ["human", "ai"]


def test_a_node_whose_process_kept_dying_fails_its_step_and_the_run(api, queued, monkeypatch):
    a = agents(api, "first", "second")
    wf = make_workflow(
        api,
        "chain",
        [agent_node("one", a["first"]), agent_node("two", a["second"])],
        [("one", "two")],
    )
    started = start(api, wf)

    async def dies(*args):
        raise ProcessDied

    monkeypatch.setattr(workflows, "run_node", dies)
    with use_model(echo()):
        for _ in range(get_settings().queue_max_attempts):
            drain_run(queued.store, started["id"])
            lapse_leases()
        # Claimed once more than it may be tried: given up on.
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["output"], out["error"]) == (
        "failed",
        None,
        f"one: {STOPPED}",
    )
    assert [(s["path"], s["status"], s["error"]) for s in out["steps"]] == [
        (["one"], "failed", STOPPED)
    ]


def test_a_long_node_holds_no_process_between_model_calls(api, queued):
    """A long single-node workflow is one item among many in a run service process's event loop:
    while its node waits on the model, nothing else of its run is held (no advance, no worker of
    its own), and the same process runs other work, here another workflow from start to end."""
    a = agents(api, "long", "quick")
    long_wf = make_workflow(api, "long", [agent_node("long", a["long"])])
    quick_wf = make_workflow(api, "quick", [agent_node("quick", a["quick"])])
    long_run, quick_run = start(api, long_wf), start(api, quick_wf)
    quick_url = f"/api/workflows/{quick_wf['id']}/runs/{quick_run['id']}"
    in_flight = []

    async def script(messages, info):
        if instructions(messages) == "long":
            in_flight.append(await asyncio.to_thread(items_in_flight))
            for _ in range(500):
                out = await asyncio.to_thread(lambda: api.get(quick_url).json())
                if out["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("the other run never got going")
            in_flight.append(await asyncio.to_thread(items_in_flight))
        yield "done"

    async def both() -> None:
        await asyncio.to_thread(enqueue_advance, queued.store, long_run["id"])
        await run_workflow(queued.store, quick_run["id"])

    with use_model(model(script)):
        asyncio.run(both())
    # Between its model calls, only its node's item was held; the other run went meanwhile.
    assert in_flight[-1] == [WorkKind.workflow_node]
    out = api.get(f"/api/workflows/{long_wf['id']}/runs/{long_run['id']}").json()
    assert (out["status"], out["output"]) == ("succeeded", "done")
    assert api.get(quick_url).json()["status"] == "succeeded"
    assert items_in_flight() == [] and queued_items() == []


def items_in_flight() -> list[WorkKind]:
    with SessionLocal() as db:
        return list(db.scalars(select(WorkItem.kind).where(WorkItem.leased_until > func.now())))


def queued_items() -> list[WorkKind]:
    with SessionLocal() as db:
        return list(db.scalars(select(WorkItem.kind)))


def test_parallel_branches_run_together_and_their_advances_are_serialised(api, queued):
    a = agents(api, "split", "left", "right", "join")
    nodes = [agent_node(k, a[k]) for k in ("split", "left", "right", "join")]
    edges = [("split", "left"), ("split", "right"), ("left", "join"), ("right", "join")]
    wf = make_workflow(api, "diamond", nodes, edges)
    started = start(api, wf)
    seen = []

    async def script(messages, info):
        name = instructions(messages)
        if name in ("left", "right"):
            seen.append(sorted(await asyncio.to_thread(items_in_flight)))
        yield f"{name}({node_input(messages[-1])})"

    advances = []
    advance = workflows.advance_run

    def counting(store_id, organisation_id, run_id):
        # Never two at once for a run: one is queued per run, and the run's row is locked.
        advances.append(sum(k == WorkKind.workflow_advance for k in items_in_flight()))
        return advance(store_id, organisation_id, run_id)

    with use_model(model(script)), pytest.MonkeyPatch.context() as patch:
        patch.setattr(workflows, "advance_run", counting)
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert out["status"] == "succeeded", out["error"]
    assert out["output"] == "join(left(split(x))\n\nright(split(x)))"
    # Both branches' nodes ran at the same time.
    assert any(kinds.count(WorkKind.workflow_node) == 2 for kinds in seen)
    assert advances and set(advances) == {1}


def delete_run(store_id: uuid.UUID, run_id: int) -> None:
    with store_session(store_id) as store:
        store.execute(delete(WorkflowRun).where(WorkflowRun.id == run_id))
        store.commit()


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
    start_step = advance_module._start_step

    def trying(store, run_id, created_by, path, node, text):
        tried.append("/".join(path))
        return start_step(store, run_id, created_by, path, node, text)

    monkeypatch.setattr(advance_module, "_start_step", trying)

    async def script(messages, info):
        name = instructions(messages)
        if name == "left":
            # As deleting it does, less its agent runs: the API's delete would wait for this
            # turn to end before it returned (`checkpoints.delete_threads`).
            await asyncio.to_thread(delete_run, queued.store, started["id"])
        if name == "right":
            # Still going once the other branch has found the run gone.
            await asyncio.sleep(0.5)
        yield "done"

    with use_model(model(script)):
        drain_run(queued.store, started["id"])
    # Neither branch's next node starts once the run has gone.
    # (left and right start together, so in either order.)
    assert tried[0] == "start" and sorted(tried[1:]) == ["left", "right"]
    assert api.get(url).status_code == 404
    # Not logged as a failure: the user deleted it.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_a_running_run_cant_be_deleted_until_it_finishes(api, db, queued, in_store):
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
        drain_run(queued.store, started["id"])
    (resp,) = refused
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "This run is still going; wait for it to finish.",
    )
    assert api.get(url).json()["status"] == "succeeded"
    assert api.delete(url).status_code == 204


def test_a_queued_run_can_be_deleted_and_then_never_starts(api, queued):
    a = agents(api, "solo")
    wf = make_workflow(api, "flow", [agent_node("solo", a["solo"])])
    started = start(api, wf)
    assert api.delete(f"/api/workflows/{wf['id']}/runs/{started['id']}").status_code == 204
    with use_model(echo()):
        drain_run(queued.store, started["id"])
    assert api.get(f"/api/workflows/{wf['id']}/runs").json() == []


def test_the_workflow_worker_its_queue_and_its_tasks_are_gone():
    # Runs are the run service's: no Celery task runs or resumes one, and no queue is theirs.
    from aios.worker import celery_app

    def queue(task: str) -> str:
        return celery_app.amqp.router.route({}, task)["queue"].name

    assert not {"aios.run_workflow", "aios.resume_agent_run"} & set(celery_app.tasks)
    routes = celery_app.conf.task_routes
    assert "workflows" not in {r["queue"] for r in routes.values()}
    assert queue("aios.ingest_document") == "ingest"


def test_a_run_ended_elsewhere_starts_nothing_more(api, queued):
    """A run failed while a node goes (deleted, or its time ran out) starts nothing more once
    the node is done."""
    a = agents(api, "slow", "after")
    wf = make_workflow(
        api,
        "flow",
        [agent_node("slow", a["slow"]), agent_node("after", a["after"])],
        [("slow", "after")],
    )
    started = start(api, wf)

    async def script(messages, info):
        if instructions(messages) == "slow":
            await asyncio.to_thread(fail_run, queued.store, started["id"], "Stopped elsewhere")
        yield "done"

    with use_model(model(script)):
        drain_run(queued.store, started["id"])
    out = api.get(f"/api/workflows/{wf['id']}/runs/{started['id']}").json()
    assert (out["status"], out["error"]) == ("failed", "Stopped elsewhere")
    assert [s["path"] for s in out["steps"]] == [["slow"]]
    assert out["steps"][0]["status"] == "failed"
