"""Caps on turns and workflow runs: model calls, tokens and cost. A turn stops before a model
call that would pass one, naming the limit and keeping what it got to. The limit in force is the
agent's or workflow's, then the organisation's, then the deployment's; editors may lower an
agent's or workflow's, only admins raise one above the organisation's.

Through the API, as a signed-in member, with the scripted model."""

import pytest
import test_workflow_runs as workflow_runs
from conftest import join_organisation, take_turn
from test_agents import agent_body, make_agent
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_chat, signed_in_as
from test_workflow_runs import queued  # noqa: F401 (a fixture)
from test_workflows import agent_node, make_workflow, workflow_body

from aios.config import get_settings
from aios.testing.fake_model import ToolCall, Usage, model, use_model

TOOLS = [{"key": "describe_data", "config": {}}]


def looping(calls: list[int], tokens: int = 10):
    """A model that says something, then calls a tool, every time it's called: it never stops
    on its own. Counts its calls in `calls`."""

    async def script(messages, info):
        calls.append(1)
        yield f"Step {len(calls)}."
        yield Usage(tokens, 1)
        yield ToolCall("describe_data", {})

    return model(script)


def capped_agent(api, name: str = "Looper", **limits) -> dict:
    agent = make_agent(api, name)
    resp = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS, limits=limits))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_an_agent_capped_at_three_model_calls_stops_before_a_fourth_keeping_its_reply(api):
    agent = capped_agent(api, turn_model_calls=3)
    chat = make_chat(api, "Ops")
    add(api, chat, "agent", agent)
    calls: list[int] = []

    with use_model(looping(calls)):
        posted = say(api, chat, "Loop", ("agent", agent))

    assert len(calls) == 3
    [(name, reply)] = replies_from(api, chat)
    assert name == "Looper"
    # What it got to, then the limit that stopped it.
    assert reply.startswith("Step 1.\n\nStep 2.\n\nStep 3.")
    assert reply.endswith("_Limit reached: at most 3 model calls per turn._")
    invocation = api.get(f"/api/chats/{chat['id']}/messages").json()["invocations"][0]
    assert invocation["id"] == posted["invocations"][0]["id"]
    assert invocation["status"] == "failed"
    assert invocation["error"] == "Limit reached: at most 3 model calls per turn."
    run = invocation["agent_run"]
    assert (run["status"], run["tool_call_count"]) == ("failed", 3)


def test_a_turn_stops_at_its_token_cap(api):
    agent = capped_agent(api, turn_tokens=250)
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    calls: list[int] = []
    with use_model(looping(calls, tokens=100)):
        take_turn(api, agent["id"], run["id"], "go")
    # 101, 202, 303 tokens: the fourth call would start past 250.
    assert len(calls) == 3
    page = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    assert page["status"] == "failed"


def test_the_limit_in_force_follows_the_agent_then_the_organisation_then_the_deployment(
    api, monkeypatch
):
    monkeypatch.setattr(get_settings(), "turn_max_model_calls", 4)
    agent = capped_agent(api)

    def calls_in_a_turn() -> int:
        run = api.post(f"/api/agents/{agent['id']}/runs").json()
        calls: list[int] = []
        with use_model(looping(calls)):
            take_turn(api, agent["id"], run["id"], "go")
        return len(calls)

    assert api.get(f"/api/agents/{agent['id']}").json()["limits_in_force"]["turn_model_calls"] == 4
    assert calls_in_a_turn() == 4
    resp = api.put("/api/organisation/limits", json={"turn_model_calls": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json()["limits"]["in_force"]["turn_model_calls"] == 3
    assert resp.json()["limits"]["defaults"]["turn_model_calls"] == 4
    assert calls_in_a_turn() == 3
    agent = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, limits={"turn_model_calls": 2})
    ).json()
    assert agent["limits"] == {"turn_model_calls": 2, "turn_tokens": None, "turn_cost": None}
    assert agent["limits_in_force"]["turn_model_calls"] == 2
    assert calls_in_a_turn() == 2


@pytest.fixture
def editor(db, user):
    return join_organisation(db, user, "editor")


def test_an_editor_may_lower_a_limit_but_raising_one_above_the_organisations_answers_403(
    api, editor
):
    agent = make_agent(api, "Bot")
    with signed_in_as(editor):
        lowered = api.put(
            f"/api/agents/{agent['id']}", json=agent_body(agent, limits={"turn_model_calls": 10})
        )
        assert lowered.status_code == 200, lowered.text
        raised = api.put(
            f"/api/agents/{agent['id']}",
            json=agent_body(agent, limits={"turn_model_calls": 201}),
        )
        assert raised.status_code == 403
        assert "Only admins can raise a limit" in raised.json()["detail"]
        assert "model calls per turn" in raised.json()["detail"]
        made = api.post("/api/agents", json={"name": "Big", "limits": {"turn_cost": 26}})
        assert made.status_code == 403
        workflow = make_workflow(api, "Flow")
        refused = api.put(
            f"/api/workflows/{workflow['id']}",
            json=workflow_body("Flow", limits={"run_cost": 1000}),
        )
        assert refused.status_code == 403
        # The organisation's limits are admins' alone.
        assert api.put("/api/organisation/limits", json={"turn_cost": 1}).status_code == 403
    assert api.get(f"/api/agents/{agent['id']}").json()["limits"]["turn_model_calls"] == 10
    # An admin may raise it; an editor may then save the agent with it kept as it is.
    raised = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, limits={"turn_model_calls": 500})
    )
    assert raised.status_code == 200, raised.text
    with signed_in_as(editor):
        kept = api.put(
            f"/api/agents/{agent['id']}",
            json=agent_body(agent, description="Edited", limits={"turn_model_calls": 500}),
        )
        assert kept.status_code == 200, kept.text
        # Saving without limits leaves them be.
        untouched = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent))
        assert untouched.json()["limits"]["turn_model_calls"] == 500


def test_a_workflow_run_stops_at_its_model_call_cap(api, queued):  # noqa: F811
    a, b = make_agent(api, "First"), make_agent(api, "Second")
    flow = make_workflow(api, "Pipeline", [agent_node("a", a), agent_node("b", b)], [("a", "b")])
    resp = api.put(
        f"/api/workflows/{flow['id']}",
        json=workflow_body(
            "Pipeline",
            [agent_node("a", a), agent_node("b", b)],
            [("a", "b")],
            limits={"run_model_calls": 1},
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["limits_in_force"]["run_model_calls"] == 1

    out = workflow_runs.run(api, queued, flow)

    assert out["status"] == "failed"
    assert "at most 1 model calls per workflow run" in out["error"]
