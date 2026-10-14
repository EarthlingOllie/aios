import asyncio

import pytest
import test_agents
from fake_model import model, replies, use_model
from test_agents import agent_body, chat_body, make_skill, spy_run
from test_workflows import agent_node, make_agent, make_workflow, workflow_body, workflow_node

from aios import checkpoints, workflows
from aios.api import workflows as workflows_api
from aios.auth import current_user
from aios.main import app
from aios.models import AgentRun, User, WorkflowRun, WorkflowRunStep
from aios.tools import TOOLS
from aios.workflows import execute_run


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

    other = User(github_id=-2, login="other")
    db.add(other)
    db.commit()
    try:
        app.dependency_overrides[current_user] = lambda: other
        assert client.get(url).status_code == 404
        assert client.post(url, json={"input": "x"}).status_code == 404
        assert client.get(f"{url}/{started['id']}").status_code == 404
        assert client.delete(f"{url}/{started['id']}").status_code == 404
    finally:
        db.delete(other)
        db.commit()
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
    # Only the subworkflow's current version counts for deleting the agent; outer keeps v1.
    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("inner", []))
    assert api.delete(f"/api/agents/{a['retired']['id']}").status_code == 204

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
