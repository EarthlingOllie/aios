"""Workflow runs that pause for a schema change request and resume once it's decided.

An agent node that calls `request_schema_change` waits for the owner, without holding a worker:
its step and then (once nothing else can run) the run become `waiting`, and its task returns.
A decision queues the run again; the worker rebuilds its graph from the steps it recorded and
gives the waiting node its outcome on its own thread.

The queued tasks are run here by hand, in order, standing in for the workers."""

import asyncio

import pytest
from fake_model import ToolCall, last_tool_results, model, tool_output, use_model
from test_agents import agent_body
from test_data_tools import provisioned
from test_schema_change_requests import CONTACTS, TOOLS, data_tables
from test_workflows import agent_node, make_agent, make_workflow

from aios import worker
from aios.datastores import data_store_for
from aios.models import DataStore
from aios.workflows import execute_run


@pytest.fixture
def store(db, user) -> DataStore:
    return provisioned(db, data_store_for(user.id))


@pytest.fixture
def queued(monkeypatch) -> list:
    """The tasks queued, as (task, args), instead of sent to the broker."""
    tasks: list = []
    for task in (
        worker.apply_schema_change_task,
        worker.resume_agent_run_task,
        worker.run_workflow_task,
    ):
        monkeypatch.setattr(task, "delay", lambda *args, task=task: tasks.append((task, args)))
    return tasks


def run_queued(queued: list) -> None:
    """Run the queued tasks (and those they queue) as the workers would."""
    while queued:
        task, args = queued.pop(0)
        task.run(*args)


class Flow:
    """The model. An agent whose base prompt is `builder` asks for `ops`, then, given the
    outcome, answers `built(<status>)`. Any other agent answers `<base prompt>(<message>)`.
    Records each tool result it's given."""

    def __init__(self, *ops: dict):
        self.ops = list(ops) or [CONTACTS]
        self.results: list = []

    async def __call__(self, messages, info):
        name, text = messages[0].content, messages[-1].content
        if not name.startswith("builder"):
            yield f"{name}({text})"
            return
        results = last_tool_results(messages)
        self.results.extend((m.name, tool_output(m)) for m in results)
        if not results:
            yield ToolCall("request_schema_change", {"ops": self.ops, "reason": "For people"})
        else:
            yield f"{name}:built({tool_output(results[-1])['status']})"


@pytest.fixture
def builder(api, store) -> dict:
    agent = make_agent(api, "Builder", base_prompt="builder")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def start(api, queued: list, workflow: dict, flow: Flow, text: str = "go") -> dict:
    """Start a run and run it as the worker would, up to where it ends or waits."""
    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": text})
    assert resp.status_code == 201, resp.text
    task, (run_id,) = queued.pop()
    assert task is worker.run_workflow_task
    with use_model(model(flow)):
        asyncio.run(execute_run(run_id))
    return read(api, workflow, run_id)


def read(api, workflow: dict, run_id: int) -> dict:
    resp = api.get(f"/api/workflows/{workflow['id']}/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def steps(out: dict) -> dict[str, dict]:
    return {"/".join(s["path"]): s for s in out["steps"]}


def test_a_requesting_node_waits_while_an_independent_branch_finishes(
    api, builder, queued, monkeypatch
):
    other = make_agent(api, "Other", base_prompt="other")
    workflow = make_workflow(
        api,
        "Builders",
        [agent_node("build", builder), agent_node("other", other)],
    )

    out = start(api, queued, workflow, Flow())

    assert out["status"] == "waiting", out["error"]
    by_path = steps(out)
    assert (by_path["other"]["status"], by_path["other"]["output"]) == ("succeeded", "other(go)")
    build = by_path["build"]
    assert build["status"] == "waiting"
    [request] = api.get("/api/schema-changes").json()
    assert request["status"] == "pending"
    assert build["waiting_on_request_id"] == request["id"]
    # Nothing is queued: the run holds no worker until it's decided.
    assert queued == []


def chain(api, builder: dict) -> dict:
    """build → after, with an independent branch, other."""
    after = make_agent(api, "After", base_prompt="after")
    other = make_agent(api, "Other", base_prompt="other")
    return make_workflow(
        api,
        "Builders",
        [agent_node("build", builder), agent_node("after", after), agent_node("other", other)],
        [("build", "after")],
    )


def test_approving_resumes_the_waiting_node_and_the_run_completes(api, builder, store, queued):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    assert "after" not in steps(waiting)
    [request] = api.get("/api/schema-changes").json()

    r = api.post(f"/api/schema-changes/{request['id']}/approve")
    assert r.status_code == 200, r.text
    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    assert data_tables(store) == ["contacts"]
    by_path = steps(out)
    assert (by_path["build"]["status"], by_path["build"]["output"]) == (
        "succeeded",
        "builder:built(applied)",
    )
    assert by_path["build"]["waiting_on_request_id"] is None
    assert by_path["after"]["input"] == "builder:built(applied)"
    assert out["output"] == "after(builder:built(applied))\n\nother(go)"
    # Nothing ran twice: the other branch's step is the one it recorded before it waited.
    assert len(out["steps"]) == 3
    assert by_path["other"] == steps(waiting)["other"]
    # The node resumed on its own agent run: one conversation, its call given the outcome.
    [(name, outcome)] = flow.results
    assert (name, outcome["status"]) == ("request_schema_change", "applied")
    build = by_path["build"]
    conversation = api.get(f"/api/agents/{build['agent_id']}/runs/{build['agent_run_id']}").json()
    assert conversation["waiting"] is False
    assert [m["type"] for m in conversation["messages"]] == ["human", "ai", "tool", "ai"]


def test_rejecting_resumes_the_waiting_node_with_the_rejection(api, builder, store, queued):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    [request] = api.get("/api/schema-changes").json()

    r = api.post(f"/api/schema-changes/{request['id']}/reject", json={"note": "Not now"})
    assert r.status_code == 200, r.text
    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    assert data_tables(store) == []
    [(_, outcome)] = flow.results
    assert (outcome["status"], outcome["note"]) == ("rejected", "Not now")
    assert steps(out)["after"]["input"] == "builder:built(rejected)"
