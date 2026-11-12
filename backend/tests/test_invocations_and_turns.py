"""Every run records why it ran, an invocation (a mention, a schedule, or a person starting it by
hand), and each of an agent run's turns is a record of its own: the version it ran on, how it
went, when, and what it cost. A run's status and counters are its turns'.

Through the API, as a signed-in member, with the scripted model."""

import json
from datetime import UTC, datetime, timedelta

import test_workflow_runs as workflow_runs
from conftest import drain_run, take_turn
from sqlalchemy import func, select
from test_agent_run_counters import searching_agent
from test_agents import agent_body, make_agent
from test_chat_agent_invocations import say
from test_chats import add, make_chat
from test_data_tools import data_agent, run_tools, store  # noqa: F401 (fixtures)
from test_scheduler import make_due, tick
from test_scheduler import make_runnable_workflow as workflow_runs_by_schedule
from test_triggers import make_trigger, schedule
from test_workflow_runs import queued  # noqa: F401 (a fixture)
from test_workflows import agent_node, make_workflow, workflow_node

from aios.datastores.models import Invocation
from aios.testing.fake_model import ToolCall, Usage, model, replies, use_model


def run_invocations(in_store, run_id: int) -> list[Invocation]:
    return in_store.scalars(select(Invocation).where(Invocation.agent_run_id == run_id)).all()


def test_a_run_started_by_hand_has_one_manual_invocation_by_whoever_started_it(api, user, in_store):
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    page = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    invocation = page["invocation"]
    assert invocation["cause"] == "manual"
    assert invocation["requested_by"] == user.id
    assert (invocation["trigger_id"], invocation["parent_id"]) == (None, None)
    assert (invocation["ancestor_ids"], invocation["depth"]) == ([], 0)
    assert [i.id for i in run_invocations(in_store, run["id"])] == [invocation["id"]]

    with use_model(replies("hi")):
        take_turn(api, agent["id"], run["id"], "hello")
    # Talking to it more doesn't make it another.
    assert len(run_invocations(in_store, run["id"])) == 1


def test_a_schedules_run_has_a_schedule_invocation_naming_its_trigger(api, db, in_store):
    agent = make_agent(api, "Newsbot")
    t = make_trigger(api, schedule("agent", agent["id"], input="What happened today?"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with use_model(replies("Not much.")):
        assert tick(now) == [t["id"]]

    (run,) = api.get(f"/api/agents/{agent['id']}/runs").json()
    invocation = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()["invocation"]
    assert (invocation["cause"], invocation["trigger_id"]) == ("schedule", t["id"])
    assert invocation["requested_by"] is None
    # Its run's turn has ended, and with it the invocation.
    assert invocation["status"] == "succeeded"
    assert invocation["finished_at"] is not None
    assert len(run_invocations(in_store, run["id"])) == 1


def test_a_workflow_agent_nodes_invocation_is_a_child_of_its_workflow_runs(api, user, queued):  # noqa: F811
    a = workflow_runs.agents(api, "draft", "critic")
    inner = make_workflow(api, "review", [agent_node("critic", a["critic"])])
    outer = make_workflow(
        api,
        "pipeline",
        [agent_node("draft", a["draft"]), workflow_node("review", inner)],
        [("draft", "review")],
    )

    out = workflow_runs.run(api, queued, outer)

    assert out["status"] == "succeeded", out["error"]
    parent = out["invocation"]
    assert (parent["cause"], parent["requested_by"]) == ("manual", user.id)
    assert (parent["parent_id"], parent["ancestor_ids"], parent["depth"]) == (None, [], 0)
    # Ended with its run.
    assert parent["status"] == "succeeded"
    # Each agent node's, at any depth, is its child: one level down, as a subworkflow is part
    # of the same run.
    for path in ("draft", "review/critic"):
        step = workflow_runs.steps(out)[path]
        node_run = api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}").json()
        child = node_run["invocation"]
        assert child["parent_id"] == parent["id"]
        assert (child["ancestor_ids"], child["depth"]) == ([parent["id"]], 1)
        assert (child["cause"], child["requested_by"]) == ("manual", user.id)
        assert child["status"] == "succeeded"


def test_a_mentions_run_has_the_mentions_invocation_and_no_other(api, user, in_store):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    with use_model(replies("Hello.")):
        posted = say(api, chat, "Hi", ("agent", agent))
    [mention] = posted["invocations"]

    (run,) = api.get(f"/api/agents/{agent['id']}/runs").json()
    invocation = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()["invocation"]
    assert (invocation["id"], invocation["cause"]) == (mention["id"], "mention")
    assert (invocation["chat_id"], invocation["requested_by"]) == (chat["id"], user.id)
    assert invocation["depth"] == 0
    assert len(run_invocations(in_store, run["id"])) == 1


def test_a_workflow_schedules_run_has_a_schedule_invocation(api, db, queued, in_store):  # noqa: F811
    workflow = workflow_runs_by_schedule(api)
    t = make_trigger(api, schedule("workflow", workflow["id"], input="Today"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    assert tick(now) == [t["id"]]

    (run,) = api.get(f"/api/workflows/{workflow['id']}/runs").json()
    with use_model(workflow_runs.echo()):
        drain_run(queued.store, run["id"])
    out = api.get(f"/api/workflows/{workflow['id']}/runs/{run['id']}").json()
    invocation = out["invocation"]
    assert (invocation["cause"], invocation["trigger_id"]) == ("schedule", t["id"])
    assert invocation["status"] == "succeeded"
    # Its agent node's is its child, with its cause.
    step = workflow_runs.steps(out)["a"]
    node_run = api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}").json()
    assert node_run["invocation"]["parent_id"] == invocation["id"]
    assert node_run["invocation"]["cause"] == "schedule"
    assert in_store.scalar(select(func.count()).where(Invocation.workflow_run_id == run["id"])) == 1


def test_each_turn_is_recorded_with_its_version_outcome_and_counters(api, monkeypatch):
    agent, runs_url = searching_agent(api, monkeypatch)
    started = api.post(runs_url).json()
    run_id, pinned = started["id"], (started["agent_version_id"], started["agent_version"])
    invocation_id = api.get(f"{runs_url}/{run_id}").json()["invocation"]["id"]

    async def searches(messages, info):
        if messages[-1].type == "human":
            yield Usage(10, 1)
            yield ToolCall("search_documents", {"query": messages[-1].content})
        else:
            yield "Done."
            yield Usage(20, 2)

    async def breaks(messages, info):
        yield Usage(5, 1)
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(searches)):
        take_turn(api, agent["id"], run_id, "first")
    # A new version since: the run stays on the one it pinned, and so do its turns.
    current = api.get(f"/api/agents/{agent['id']}").json()
    edited = api.put(f"/api/agents/{agent['id']}", json=agent_body(current, base_prompt="New."))
    assert edited.json()["current_version"]["id"] != pinned[0]
    with use_model(model(breaks)):
        take_turn(api, agent["id"], run_id, "second")

    run = api.get(f"{runs_url}/{run_id}").json()
    first, second = run["turns"]
    assert {(t["agent_version_id"], t["agent_version"]) for t in run["turns"]} == {pinned}
    assert {t["invocation_id"] for t in run["turns"]} == {invocation_id}
    assert (first["status"], first["error"]) == ("succeeded", None)
    assert (first["model_calls"], first["tool_calls"]) == (2, 1)
    assert (first["input_tokens"], first["output_tokens"]) == (30, 3)
    assert (second["status"], second["error"]) == ("failed", "model overloaded")
    # Its one call never finished, so its usage never arrived.
    assert (second["model_calls"], second["tool_calls"], second["input_tokens"]) == (0, 0, 0)
    assert first["started_at"] <= first["finished_at"] <= second["started_at"]

    # The run's status and times are its latest turn's; its counters add up over them all.
    assert run["status"] == "failed"
    assert (run["started_at"], run["finished_at"]) == (
        second["started_at"],
        second["finished_at"],
    )
    assert (run["tool_call_count"], run["input_tokens"], run["output_tokens"]) == (1, 30, 3)
    (listed,) = api.get(runs_url).json()
    for key in ("status", "started_at", "finished_at", "tool_call_count", "input_tokens"):
        assert listed[key] == run[key]


def test_agents_read_runs_and_turns_from_reporting_views_without_any_text(
    api,
    store,  # noqa: F811
    data_agent,  # noqa: F811
    monkeypatch,
):
    secret = "The plan is codenamed Bluebird"
    bot, runs_url = searching_agent(api, monkeypatch)
    run_id = api.post(runs_url).json()["id"]

    async def script(messages, info):
        if messages[-1].type == "human":
            yield Usage(10, 1)
            yield ToolCall("search_documents", {"query": secret})
        else:
            yield f"{secret}, confirmed."
            yield Usage(20, 2)

    with use_model(model(script)):
        take_turn(api, bot["id"], run_id, secret)

    described, runs, turns = run_tools(
        api,
        data_agent,
        ToolCall("describe_data", {}),
        ToolCall("query_data", {"sql": "SELECT * FROM reporting.runs ORDER BY run_id"}),
        ToolCall("query_data", {"sql": "SELECT * FROM reporting.turns ORDER BY turn_id"}),
    )

    views = {(t["schema"], t["name"]) for t in described["tables"]}
    assert {("reporting", "runs"), ("reporting", "turns")} <= views
    reported_runs = [dict(zip(runs["columns"], row, strict=True)) for row in runs["rows"]]
    (reported,) = [r for r in reported_runs if (r["kind"], r["run_id"]) == ("agent", run_id)]
    assert (reported["kind"], reported["agent_id"], reported["cause"]) == (
        "agent",
        bot["id"],
        "manual",
    )
    assert (reported["status"], reported["turns"], reported["model_calls"]) == ("succeeded", 1, 2)
    assert (reported["tool_calls"], reported["input_tokens"], reported["output_tokens"]) == (
        1,
        30,
        3,
    )
    reported_turns = [dict(zip(turns["columns"], row, strict=True)) for row in turns["rows"]]
    (turn,) = [t for t in reported_turns if t["run_id"] == run_id]
    assert (turn["status"], turn["model_calls"], turn["input_tokens"]) == ("succeeded", 2, 30)
    # Identifiers, counts, times and statuses only: nothing anyone or any agent wrote.
    everything = json.dumps([runs, turns])
    assert "Bluebird" not in everything
    texts = {
        c["name"]
        for t in described["tables"]
        # The views of runs and turns (the audit log's has its own test, in test_audit_log.py).
        if t["schema"] == "reporting" and t["name"] in ("runs", "turns")
        for c in t["columns"]
        if c["type"] == "text"
    }
    assert texts == {"kind", "cause", "status"}
