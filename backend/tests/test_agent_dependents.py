"""What depends on an agent, and deleting it only once that's resolved.

`GET /api/agents/{id}/dependents` lists what would break if the agent went: its chat runs that
are running or waiting on a schema change request, the workflow runs still going that run it (at
any depth), the live workflows whose current version runs it (directly or through a nested
workflow), and the schedules that would start either. `DELETE /api/agents/{id}` refuses (409,
with that list) until each is resolved in the request: the runs it names are cancelled, and each
workflow is deactivated (its schedules turned off) or has been edited to stop running the agent.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import func, update
from test_data_tools import as_owner
from test_scheduler import tick
from test_schema_change_requests import (  # noqa: F401
    CONTACTS,
    Script,
    agent,
    data_tables,
    start,
    store,
)
from test_workflow_pause import Flow, builder, queued, steps  # noqa: F401
from test_workflow_pause import read as read_flow
from test_workflow_pause import start as start_flow
from test_workflows import agent_node, make_agent, make_workflow, workflow_body, workflow_node

from aios import checkpoints, workflows
from aios.api import agents as agents_api
from aios.api.skills import lock_live
from aios.db import SessionLocal
from aios.models import Agent, Workflow, WorkflowRun, WorkflowRunStatus
from aios.testing.fake_model import model, use_model


def dependents(api, agent: dict) -> dict:  # noqa: F811
    resp = api.get(f"/api/agents/{agent['id']}/dependents")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_chat_run_waiting_on_a_request_depends_on_its_agent(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    run, request = start(api, agent, Script(CONTACTS))
    # A run that isn't waiting (or running) doesn't.
    idle = api.post(f"/api/agents/{agent['id']}/runs").json()

    found = dependents(api, agent)

    assert [(r["id"], r["waiting"], r["running"]) for r in found["agent_runs"]] == [
        (run["id"], True, False)
    ]
    # With the request it waits on, to open on the Data page.
    assert found["agent_runs"][0]["requests"] == [
        {"id": request["id"], "summary": "Create table data.contacts", "status": "pending"}
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
    # Which node waits, however deep, and on what.
    [listed] = found["workflow_runs"]
    assert listed["waiting_nodes"] == ["middle/inner/build"]
    [request] = api.get("/api/schema-changes").json()
    assert [r["id"] for r in listed["requests"]] == [request["id"]]
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


# --- Deleting --------------------------------------------------------------------------------


def delete(api, agent: dict, **resolutions):  # noqa: F811
    """Delete the agent, resolving what depends on it as `resolutions` say."""
    return api.request("DELETE", f"/api/agents/{agent['id']}", json=resolutions or None)


def cancelling(found: dict) -> dict:
    """The resolutions that cancel every run listed."""
    return {
        "cancel_agent_runs": [r["id"] for r in found["agent_runs"]],
        "cancel_workflow_runs": [r["id"] for r in found["workflow_runs"]],
    }


def test_an_agent_nothing_depends_on_is_deleted_as_before(api, builder):  # noqa: F811
    run = api.post(f"/api/agents/{builder['id']}/runs").json()

    assert delete(api, builder).status_code == 204

    assert api.get(f"/api/agents/{builder['id']}").json()["archived_at"] is not None
    assert api.get(f"/api/agents/{builder['id']}/runs/{run['id']}").status_code == 404


def test_deleting_is_refused_with_the_dependents_until_theyre_resolved(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    run, request = start(api, agent, Script(CONTACTS))

    resp = delete(api, agent)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["dependents"] == dependents(api, agent)
    assert [r["id"] for r in detail["dependents"]["agent_runs"]] == [run["id"]]
    assert "1 chat run" in detail["message"]
    # Nothing was done: the agent, its run and its request are as they were.
    assert api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()["waiting"] is True
    assert api.get(f"/api/schema-changes/{request['id']}").json()["status"] == "pending"
    assert api.get("/api/notifications").json()["unread"] == 1


def test_cancelling_a_waiting_chat_run_cancels_its_request_and_nothing_resumes(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    _, request = start(api, agent, Script(CONTACTS))

    resp = delete(api, agent, **cancelling(dependents(api, agent)))

    assert resp.status_code == 204, resp.text
    assert api.get(f"/api/schema-changes/{request['id']}").json()["status"] == "cancelled"
    assert api.get("/api/notifications").json()["unread"] == 0
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 409
    assert queued == []
    assert data_tables(store) == []


def test_cancelling_a_waiting_workflow_run_ends_it_and_cancels_its_request(
    api,
    builder,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    waiting = start_flow(api, queued, workflow, Flow())
    # Edited so only the run depends on it.
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builds", []))
    [request] = api.get("/api/schema-changes").json()

    assert delete(api, builder).status_code == 409
    resp = delete(api, builder, **cancelling(dependents(api, builder)))

    assert resp.status_code == 204, resp.text
    out = read_flow(api, workflow, waiting["id"])
    assert out["status"] == "failed"
    assert "Builder" in out["error"] and "deleted" in out["error"]
    assert steps(out)["build"]["status"] == "failed"
    assert api.get(f"/api/schema-changes/{request['id']}").json()["status"] == "cancelled"
    assert api.get("/api/notifications").json()["unread"] == 0
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 409
    # Nothing resumes it.
    assert queued == []
    assert data_tables(store) == []


def test_cancelling_a_running_workflow_run_ends_it_so_its_worker_stops(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    run_id = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"}).json()["id"]
    queued.clear()
    # A worker has claimed it and is beating.
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(status=WorkflowRunStatus.running, heartbeat_at=func.now())
        )
        db.commit()
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builds", []))
    found = dependents(api, builder)
    assert [(r["id"], r["status"]) for r in found["workflow_runs"]] == [(run_id, "running")]

    assert delete(api, builder, **cancelling(found)).status_code == 204

    assert read_flow(api, workflow, run_id)["status"] == "failed"
    # Its worker's next beat finds it ended, and stops it (`aios.workflows._Heartbeat`).
    assert workflows._beat(run_id) is False


def test_a_running_run_cancelled_mid_node_starts_no_more_nodes(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    first = make_agent(api, "First", base_prompt="first")
    then = make_agent(api, "Then", base_prompt="then")
    workflow = make_workflow(
        api, "Pair", [agent_node("first", first), agent_node("then", then)], [("first", "then")]
    )
    run_id = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"}).json()["id"]
    queued.clear()

    async def script(messages, info):
        if messages[0].content == "first":
            # Cancelled while its first node runs (as deleting an agent it runs would).
            with SessionLocal() as db:
                workflows.cancel_runs(db, [run_id], "Cancelled")
                db.commit()
        yield f"{messages[0].content}({messages[-1].content})"

    with use_model(model(script)):
        asyncio.run(workflows.execute_run(run_id))

    out = read_flow(api, workflow, run_id)
    assert (out["status"], out["error"]) == ("failed", "Cancelled")
    assert "then" not in steps(out)


def test_a_run_not_named_in_the_request_isnt_cancelled(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    first, _ = start(api, agent, Script(CONTACTS))
    resolutions = cancelling(dependents(api, agent))
    # Another starts waiting after the user saw the list: they haven't agreed to cancel it.
    second, request = start(api, agent, Script({**CONTACTS, "name": "places"}))

    resp = delete(api, agent, **resolutions)

    assert resp.status_code == 409
    listed = [r["id"] for r in resp.json()["detail"]["dependents"]["agent_runs"]]
    assert sorted(listed) == sorted([first["id"], second["id"]])
    assert api.get(f"/api/schema-changes/{request['id']}").json()["status"] == "pending"
    assert api.get(f"/api/agents/{agent['id']}").status_code == 200


def test_deactivating_a_workflow_turns_off_its_schedules_and_it_cant_start(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    daily = schedule(api, "workflow", workflow)
    other = make_workflow(api, "Other", [agent_node("o", make_agent(api, "Other"))])
    unrelated = schedule(api, "workflow", other)

    assert delete(api, builder).status_code == 409
    resp = delete(api, builder, deactivate_workflows=[workflow["id"]])

    assert resp.status_code == 204, resp.text
    turned_off = api.get(f"/api/triggers/{daily['id']}").json()
    assert (turned_off["enabled"], turned_off["next_run_at"]) == (False, None)
    assert api.get(f"/api/triggers/{unrelated['id']}").json()["enabled"] is True
    # A schedule that would have fired by now doesn't.
    fired = tick(datetime.now(UTC) + timedelta(days=2))
    assert unrelated["id"] in fired and daily["id"] not in fired
    # Nor can a run be started by hand: it runs an archived agent.
    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"})
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"]
    # The workflow itself stays, to be edited.
    assert api.get(f"/api/workflows/{workflow['id']}").status_code == 200


def test_a_workflow_left_to_be_edited_blocks_the_delete_until_it_is(
    api,
    builder,  # noqa: F811
):
    inner = make_workflow(api, "Inner", [agent_node("build", builder)])
    outer = make_workflow(api, "Outer", [workflow_node("inner", inner)])
    # Outer is deactivated; Inner is to be edited, which hasn't happened yet.
    resolutions = {"deactivate_workflows": [outer["id"]]}

    resp = delete(api, builder, **resolutions)
    assert resp.status_code == 409
    assert "Inner" in resp.json()["detail"]["message"]
    assert "Outer" not in resp.json()["detail"]["message"]
    assert api.get(f"/api/agents/{builder['id']}").status_code == 200

    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("Inner", []))
    # Outer still pins Inner v1, which runs the agent: it's still a dependent, deactivated here.
    assert [w["name"] for w in dependents(api, builder)["workflows"]] == ["Outer"]
    assert delete(api, builder, **resolutions).status_code == 204


def test_a_run_starting_during_the_delete_is_seen_or_waits_and_then_cant(
    api,
    builder,  # noqa: F811
    queued,  # noqa: F811
):
    """A run that starts as the agent is deleted either is seen by the delete (which refuses,
    as the user hasn't agreed to cancel it), or waits for it, and then can't start: it would run
    a deleted agent."""
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    resolutions = {"deactivate_workflows": [workflow["id"]]}
    result: dict = {}

    with SessionLocal() as session:
        # A run being started: it holds the workflow's share lock until it commits.
        assert lock_live(session, Workflow, [workflow["id"]], read=True)
        deleter = threading.Thread(
            target=lambda: result.update(resp=delete(api, builder, **resolutions))
        )
        deleter.start()
        deleter.join(timeout=0.5)
        assert deleter.is_alive(), "the delete should wait for the run being started"
        session.add(
            WorkflowRun(
                workflow_id=workflow["id"],
                workflow_version_id=workflow["current_version"]["id"],
                input="go",
            )
        )
        session.commit()
    deleter.join(timeout=10)

    assert result["resp"].status_code == 409
    [run] = result["resp"].json()["detail"]["dependents"]["workflow_runs"]

    # One started once the delete has locked what it deletes waits, then is refused.
    with SessionLocal() as session:
        assert lock_live(session, Agent, [builder["id"]])
        assert lock_live(session, Workflow, [workflow["id"]])
        session.get(Agent, builder["id"]).archived_at = func.now()
        starter = threading.Thread(
            target=lambda: result.update(
                start=api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"})
            )
        )
        starter.start()
        starter.join(timeout=0.5)
        assert starter.is_alive(), "starting a run should wait for the delete"
        session.commit()
    starter.join(timeout=10)

    assert result["start"].status_code == 409
    runs = api.get(f"/api/workflows/{workflow['id']}/runs").json()
    assert [r["id"] for r in runs] == [run["id"]]


def running(workflow: dict, heartbeat=None) -> int:
    """A run of the workflow that a worker has claimed (beating now, or at `heartbeat`)."""
    with SessionLocal() as db:
        run = WorkflowRun(
            workflow_id=workflow["id"],
            workflow_version_id=workflow["current_version"]["id"],
            input="go",
            status=WorkflowRunStatus.running,
            heartbeat_at=func.now() if heartbeat is None else heartbeat,
        )
        db.add(run)
        db.commit()
        return run.id


def test_a_node_starting_as_its_agent_is_deleted_waits_rather_than_deadlocking(
    api,
    builder,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    run_id = running(workflow)
    with SessionLocal() as db:
        [node] = workflows.load_graph(db, workflow["current_version"]["id"]).nodes
    result: dict = {}

    def start_node():
        try:
            result["step"] = workflows._start_step(run_id, None, ("build",), node, "go")
        except Exception as exc:
            result["error"] = exc

    with SessionLocal() as session:
        # The delete: it holds the agent, then cancels the run.
        assert lock_live(session, Agent, [builder["id"]])
        starter = threading.Thread(target=start_node)
        starter.start()
        starter.join(timeout=0.5)
        assert starter.is_alive(), "the node should wait for the agent"
        workflows.cancel_runs(session, [run_id], "Cancelled")
        session.get(Agent, builder["id"]).archived_at = func.now()
        session.commit()
    starter.join(timeout=10)

    assert isinstance(result.get("error"), workflows.RunError), result
    assert str(result["error"]) == "The run has ended"


def test_cancelling_a_chat_run_mid_turn_stops_the_turn(
    api,
    agent,  # noqa: F811
):
    run = api.post(f"/api/agents/{agent['id']}/runs").json()

    async def scenario() -> None:
        async with checkpoints.turn(run["id"]) as saver:
            found = await asyncio.to_thread(dependents, api, agent)
            resp = await asyncio.wait_for(
                asyncio.to_thread(delete, api, agent, **cancelling(found)), timeout=10
            )
            assert resp.status_code == 204, resp.text
            # Its connection, which holds the turn, was ended: the turn's next write fails.
            with pytest.raises(psycopg.OperationalError):
                await saver.conn.execute("SELECT 1")

    asyncio.run(scenario())


def test_a_turn_starting_on_a_run_deleted_meanwhile_writes_nothing(
    api,
    user,
    agent,  # noqa: F811
):
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with SessionLocal() as db:
        ctx = agents_api.load_chat_context(db, agent["id"], run["id"])
    # Deleted after the turn loaded the run, before it took the run's turn lock.
    assert delete(api, agent).status_code == 204

    async def turn() -> list[dict]:
        stream = agents_api.turn_stream(ctx, run["id"], HumanMessage("hi"))
        return [chunk async for chunk in stream]

    with use_model(model(Script(CONTACTS))):
        chunks = asyncio.run(turn())

    assert any(c["type"] == "error" for c in chunks)
    # No thread left behind for a run that's gone.
    assert checkpoints.history(run["id"]) == []


def test_a_turn_lock_in_another_database_isnt_a_turn_here(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
):
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with as_owner(store) as conn:
        # The same advisory lock key, held on the store's database.
        conn.execute("SELECT pg_advisory_lock(%s, %s)", (checkpoints._TURN_LOCK, run["id"]))
        assert dependents(api, agent)["agent_runs"] == []


def test_a_run_whose_worker_died_is_failed_not_left_going(
    api,
    builder,  # noqa: F811
):
    workflow = make_workflow(api, "Builds", [agent_node("build", builder)])
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builds", []))
    run_id = running(workflow, heartbeat=func.now() - timedelta(hours=1))

    assert dependents(api, builder)["workflow_runs"] == []
    assert delete(api, builder).status_code == 204

    # Failed, as reading it would: if its worker was only slow, its next beat stops it.
    assert workflows._beat(run_id) is False
