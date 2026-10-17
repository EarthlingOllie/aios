"""What depends on an agent, and deleting it only once that's resolved.

`GET /api/agents/{id}/dependents` lists what would break if the agent went: its chat runs that
are running or waiting on a schema change request, the workflow runs still going that run it (at
any depth), the live workflows whose current version runs it (directly or through a nested
workflow), and the schedules that would start either. `DELETE /api/agents/{id}` refuses (409,
with that list) until each is resolved in the request: the runs it names are cancelled, and each
workflow is deactivated (its schedules turned off) or has been edited to stop running the agent.
"""

import asyncio

from sqlalchemy import update
from test_schema_change_requests import (  # noqa: F401
    CONTACTS,
    Script,
    agent,
    start,
    store,
)
from test_workflow_pause import Flow, builder, queued  # noqa: F401
from test_workflow_pause import start as start_flow
from test_workflows import agent_node, make_agent, make_workflow, workflow_body, workflow_node

from aios import checkpoints
from aios.db import SessionLocal
from aios.models import WorkflowRun, WorkflowRunStatus


def dependents(api, agent: dict) -> dict:  # noqa: F811
    resp = api.get(f"/api/agents/{agent['id']}/dependents")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_chat_run_waiting_on_a_request_depends_on_its_agent(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    run, _ = start(api, agent, Script(CONTACTS))
    # A run that isn't waiting (or running) doesn't.
    idle = api.post(f"/api/agents/{agent['id']}/runs").json()

    found = dependents(api, agent)

    assert [(r["id"], r["waiting"], r["running"]) for r in found["agent_runs"]] == [
        (run["id"], True, False)
    ]
    assert idle["id"] not in [r["id"] for r in found["agent_runs"]]
    assert found["workflow_runs"] == []
    assert found["workflows"] == []
    assert found["triggers"] == []


def test_a_chat_run_with_a_turn_underway_depends_on_its_agent(
    api,
    agent,  # noqa: F811
):
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def during_a_turn() -> dict:
        async with checkpoints.turn(run["id"]):
            return await asyncio.to_thread(dependents, api, agent)

    found = asyncio.run(during_a_turn())

    assert [(r["id"], r["waiting"], r["running"]) for r in found["agent_runs"]] == [
        (run["id"], False, True)
    ]
    assert dependents(api, agent)["agent_runs"] == []


def test_a_waiting_workflow_run_and_its_workflow_depend_on_the_agent(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    run = start_flow(api, queued, workflow, Flow())
    assert run["status"] == "waiting"

    found = dependents(api, builder)

    assert [
        (r["id"], r["workflow_id"], r["workflow_name"], r["status"]) for r in found["workflow_runs"]
    ] == [(run["id"], workflow["id"], "Builds", "waiting")]
    assert [(w["id"], w["name"], w["direct"]) for w in found["workflows"]] == [
        (workflow["id"], "Builds", True)
    ]
    # The node's own agent run is the workflow run's, not a chat.
    assert found["agent_runs"] == []


def test_a_nested_workflow_and_its_run_depend_on_the_agent_at_any_depth(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    inner = make_workflow(api, "Inner", [agent_node("build", builder)])
    middle = make_workflow(api, "Middle", [workflow_node("inner", inner)])
    outer = make_workflow(api, "Outer", [workflow_node("middle", middle)])
    unrelated = make_workflow(api, "Unrelated", [agent_node("o", make_agent(api, "Other"))])
    run = start_flow(api, queued, outer, Flow())
    assert run["status"] == "waiting"

    found = dependents(api, builder)

    assert [r["id"] for r in found["workflow_runs"]] == [run["id"]]
    assert sorted((w["name"], w["direct"]) for w in found["workflows"]) == [
        ("Inner", True),
        ("Middle", False),
        ("Outer", False),
    ]
    assert unrelated["id"] not in [w["id"] for w in found["workflows"]]


def end(run: dict) -> None:
    """The run ends (fails, say) behind the API's back."""
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run["id"])
            .values(status=WorkflowRunStatus.failed)
        )
        db.commit()


def test_only_current_versions_and_runs_still_going_count(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    waiting = start_flow(api, queued, workflow, Flow())
    # Edited to stop running it: the workflow no longer depends on it, but its run still does.
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builds", []))

    found = dependents(api, builder)

    assert found["workflows"] == []
    assert [r["id"] for r in found["workflow_runs"]] == [waiting["id"]]
    end(waiting)
    assert dependents(api, builder)["workflow_runs"] == []


def schedule(api, kind: str, target: dict, enabled: bool = True) -> dict:
    resp = api.post(
        "/api/triggers",
        json={
            "target_kind": kind,
            "target_id": target["id"],
            "cron": "0 9 * * *",
            "input": "go",
            "enabled": enabled,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_the_schedules_that_would_start_its_runs_are_listed(api, builder):  # noqa: F811
    own = schedule(api, "agent", builder)
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    daily = schedule(api, "workflow", workflow)
    paused = schedule(api, "workflow", workflow, enabled=False)

    found = dependents(api, builder)

    assert [(t["id"], t["enabled"]) for t in found["triggers"]] == [(own["id"], True)]
    [listed] = found["workflows"]
    assert sorted((t["id"], t["enabled"]) for t in listed["triggers"]) == [
        (daily["id"], True),
        (paused["id"], False),
    ]


def test_an_agent_nothing_depends_on_has_no_dependents(api, builder):  # noqa: F811
    api.post(f"/api/agents/{builder['id']}/runs")
    assert dependents(api, builder) == {
        "agent_runs": [],
        "workflow_runs": [],
        "workflows": [],
        "triggers": [],
    }
