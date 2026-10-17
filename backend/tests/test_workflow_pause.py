"""Workflow runs that pause for a schema change request and resume once it's decided.

An agent node that calls `request_schema_change` waits for the owner, without holding a worker:
its step and then (once nothing else can run) the run become `waiting`, and its task returns.
A decision queues the run again; the worker rebuilds its graph from the steps it recorded and
gives the waiting node its outcome on its own thread.

The queued tasks are run here by hand, in order, standing in for the workers."""

import asyncio
import threading
from datetime import timedelta

import pytest
from fake_model import ToolCall, last_tool_results, model, tool_output, use_model
from sqlalchemy import func, update
from test_agents import agent_body
from test_data_tools import provisioned
from test_schema_change_requests import CONTACTS, TOOLS, data_tables
from test_workflows import (
    agent_node,
    count_queries,
    make_agent,
    make_workflow,
    workflow_body,
    workflow_node,
)

from aios import worker, workflows
from aios.config import get_settings
from aios.datastores import data_store_for
from aios.db import SessionLocal
from aios.models import (
    DataStore,
    SchemaChangeRequest,
    SchemaChangeStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from aios.tools import TOOLS as TOOL_LIBRARY
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
    """The model. An agent whose base prompt is `builder` asks for `ops` (`builder-<name>`: a
    table `<name>`), then, given the outcome, answers `<base prompt>:built(<status>)`. Any other
    agent answers `<base prompt>(<message>)`. Records each tool result it's given."""

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
            _, _, table = name.partition("-")
            ops = [{**CONTACTS, "name": table}] if table else self.ops
            yield ToolCall("request_schema_change", {"ops": ops, "reason": "For people"})
        else:
            yield f"{name}:built({tool_output(results[-1])['status']})"


@pytest.fixture
def builder(api, store) -> dict:
    agent = make_agent(api, "Builder", base_prompt="builder")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def start(api, queued: list, workflow: dict, script, text: str = "go") -> dict:
    """Start a run and run it as the worker would (on `script`, e.g. a `Flow`), up to where it
    ends or waits."""
    resp = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": text})
    assert resp.status_code == 201, resp.text
    task, (run_id,) = queued.pop()
    assert task is worker.run_workflow_task
    with use_model(model(script)):
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
    assert build["waiting_on_request_ids"] == [request["id"]]
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
    assert by_path["build"]["waiting_on_request_ids"] == []
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


def test_a_long_wait_isnt_failed_for_its_heartbeat_or_the_time_limit(
    api, db, builder, store, queued
):
    """Waiting holds no worker, so it beats no heartbeat; and only running counts towards the
    run's time limit. How long it waited is recorded."""
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    assert waiting["waiting_since"] is not None
    limit = get_settings().workflow_run_time_limit
    # It ran for a minute, then waited far longer than the heartbeat's lease and the limit.
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == waiting["id"])
        .values(
            started_at=func.now() - timedelta(seconds=2 * limit + 60),
            waiting_since=func.now() - timedelta(seconds=2 * limit),
            heartbeat_at=func.now() - timedelta(seconds=2 * limit),
        )
    )
    db.commit()

    # Read (which fails runs whose worker stopped) and listed: still waiting.
    assert read(api, workflow, waiting["id"])["status"] == "waiting"
    listed = api.get(f"/api/workflows/{workflow['id']}/runs").json()
    assert [r["status"] for r in listed] == ["waiting"]

    [request] = api.get("/api/schema-changes").json()
    api.post(f"/api/schema-changes/{request['id']}/approve")
    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    assert out["waiting_since"] is None
    assert 2 * limit <= out["waited_seconds"] < 2 * limit + 60


def test_a_run_resumes_with_only_what_was_left_of_its_time_limit(
    api, db, builder, store, queued, monkeypatch
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    limit = get_settings().workflow_run_time_limit
    # It had run for all but a moment of its limit before it waited.
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == waiting["id"])
        .values(
            started_at=func.now() - timedelta(seconds=limit + 60),
            waiting_since=func.now() - timedelta(seconds=60),
        )
    )
    db.commit()
    [request] = api.get("/api/schema-changes").json()
    api.post(f"/api/schema-changes/{request['id']}/approve")

    async def slow(messages, info):
        await asyncio.sleep(2)
        async for item in flow(messages, info):
            yield item

    with use_model(model(slow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert (out["status"], out["error"]) == ("failed", workflows.time_limit_error())


def decide(api, queued: list, flow: Flow, decision: str = "approve") -> None:
    """Decide the one pending request, and run what that queues."""
    [request] = [r for r in api.get("/api/schema-changes").json() if r["status"] == "pending"]
    r = api.post(f"/api/schema-changes/{request['id']}/{decision}", json={})
    assert r.status_code == 200, r.text
    with use_model(model(flow)):
        run_queued(queued)


def test_editing_the_agent_while_the_run_waits_doesnt_change_what_it_resumes_on(
    api, builder, store, queued
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    api.put(f"/api/agents/{builder['id']}", json=agent_body(builder, base_prompt="builder-v2"))

    decide(api, queued, flow)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    assert steps(out)["build"]["output"] == "builder:built(applied)"


def test_deleting_the_agent_while_the_run_waits_fails_its_step_on_resume(
    api, builder, store, queued
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    # It can only be deleted once no workflow's current version uses it.
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builders", []))
    assert api.delete(f"/api/agents/{builder['id']}").status_code == 204

    decide(api, queued, flow)

    out = read(api, workflow, waiting["id"])
    reason = "Agent Builder v2 can't run: it has been deleted"
    assert (out["status"], out["error"]) == ("failed", f"build: {reason}")
    build = steps(out)["build"]
    assert (build["status"], build["error"]) == ("failed", reason)
    assert "after" not in steps(out)


def test_a_tool_that_left_the_library_fails_the_step_on_resume(
    api, builder, store, queued, monkeypatch
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    monkeypatch.delitem(TOOL_LIBRARY, "describe_data")

    decide(api, queued, flow)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "failed"
    build = steps(out)["build"]
    assert build["status"] == "failed"
    assert build["error"].startswith("Agent Builder v2 can't run:")
    assert "describe_data" in build["error"]
    # Its request has been dealt with: the conversation isn't left waiting for it.
    assert conversation_of(api, build)["waiting"] is False


def conversation_of(api, step: dict) -> dict:
    return api.get(f"/api/agents/{step['agent_id']}/runs/{step['agent_run_id']}").json()


def test_a_run_ending_while_its_request_is_applied_leaves_no_conversation_waiting(
    api, builder, store, queued
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    [request] = api.get("/api/schema-changes").json()
    api.post(f"/api/schema-changes/{request['id']}/approve")
    # Before it's applied, the run is resumed by another decision and ends: failed, say.
    with SessionLocal() as db:
        db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == waiting["id"])
            .values(status=WorkflowRunStatus.running)
        )
        db.commit()
    workflows.fail_run(waiting["id"], "Something else broke")

    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "failed"
    assert data_tables(store) == ["contacts"]
    assert conversation_of(api, steps(out)["build"])["waiting"] is False


def test_deleting_a_waiting_run_cancels_its_pending_request(api, builder, store, queued):
    workflow = chain(api, builder)
    waiting = start(api, queued, workflow, Flow())

    r = api.delete(f"/api/workflows/{workflow['id']}/runs/{waiting['id']}")

    assert r.status_code == 204, r.text
    [request] = api.get("/api/schema-changes").json()
    assert request["status"] == "cancelled"
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 409
    assert api.get("/api/notifications").json()["unread"] == 0
    assert queued == []
    assert data_tables(store) == []


def test_deleting_a_waiting_run_whose_agent_is_gone_still_cancels_its_request(
    api, builder, store, queued
):
    """The node's agent run went with its agent, so only the step knows the request."""
    workflow = chain(api, builder)
    waiting = start(api, queued, workflow, Flow())
    api.put(f"/api/workflows/{workflow['id']}", json=workflow_body("Builders", []))
    assert api.delete(f"/api/agents/{builder['id']}").status_code == 204

    r = api.delete(f"/api/workflows/{workflow['id']}/runs/{waiting['id']}")

    assert r.status_code == 204, r.text
    [request] = api.get("/api/schema-changes").json()
    assert request["status"] == "cancelled"
    assert api.get("/api/notifications").json()["unread"] == 0


def test_a_request_past_the_expiry_resumes_its_waiting_run_with_expired(
    api, builder, store, queued
):
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    [request] = api.get("/api/schema-changes").json()
    age = timedelta(days=get_settings().schema_change_request_expiry_days, minutes=1)
    with SessionLocal() as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .values(created_at=func.now() - age)
        )
        db.commit()

    worker.expire_schema_change_requests_task.run()
    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    [(_, outcome)] = flow.results
    assert outcome["status"] == "expired"
    assert steps(out)["after"]["input"] == "builder:built(expired)"
    assert data_tables(store) == []
    assert api.get("/api/notifications").json()["unread"] == 0


def test_a_nested_workflows_waiting_node_pauses_and_resumes_the_top_level_run(
    api, builder, store, queued
):
    inner = chain(api, builder)
    publish = make_agent(api, "Publish", base_prompt="publish")
    outer = make_workflow(
        api,
        "Pipeline",
        [workflow_node("review", inner), agent_node("publish", publish)],
        [("review", "publish")],
    )
    flow = Flow()

    waiting = start(api, queued, outer, flow)

    assert waiting["status"] == "waiting", waiting["error"]
    by_path = steps(waiting)
    assert by_path["review"]["status"] == "waiting"
    assert by_path["review"]["waiting_on_request_ids"] == []
    assert by_path["review/build"]["status"] == "waiting"
    assert len(by_path["review/build"]["waiting_on_request_ids"]) == 1
    assert by_path["review/other"]["status"] == "succeeded"
    assert "publish" not in by_path

    decide(api, queued, flow)

    out = read(api, outer, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    by_path = steps(out)
    assert by_path["review"]["output"] == "after(builder:built(applied))\n\nother(go)"
    assert out["output"] == "publish(after(builder:built(applied))\n\nother(go))"
    assert len(out["steps"]) == 5


def test_a_node_failing_while_another_waits_fails_the_run_and_cancels_the_request(
    api, builder, store, queued
):
    broken = make_agent(api, "Broken", base_prompt="broken")
    workflow = make_workflow(
        api, "Builders", [agent_node("build", builder), agent_node("broken", broken)]
    )
    flow = Flow()

    async def breaks(messages, info):
        if messages[0].content == "broken":
            await asyncio.sleep(0.5)  # once the other node waits
            raise RuntimeError("it broke")
        async for item in flow(messages, info):
            yield item

    out = start(api, queued, workflow, breaks)

    assert (out["status"], out["error"]) == ("failed", "broken: it broke")
    build = steps(out)["build"]
    assert (build["status"], build["error"]) == (
        "failed",
        "The run stopped before this step finished.",
    )
    [request] = api.get("/api/schema-changes").json()
    assert request["status"] == "cancelled"
    conversation = api.get(f"/api/agents/{build['agent_id']}/runs/{build['agent_run_id']}").json()
    assert conversation["waiting"] is False
    assert queued == []


def test_a_decision_made_while_the_run_is_running_isnt_missed(api, builder, store, queued):
    """Its queuing finds the run running, and leaves it; the run, pausing, queues itself."""
    places = make_agent(api, "Places", base_prompt="builder-places")
    places = api.put(f"/api/agents/{places['id']}", json=agent_body(places, tools=TOOLS)).json()
    workflow = make_workflow(
        api, "Builders", [agent_node("contacts", builder), agent_node("places", places)]
    )
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    assert [s["status"] for s in waiting["steps"]] == ["waiting", "waiting"]
    by_table = {r["summary"]: r for r in api.get("/api/schema-changes").json()}
    contacts = by_table["Create table data.contacts"]
    places_request = by_table["Create table data.places"]
    api.post(f"/api/schema-changes/{contacts['id']}/reject", json={})

    async def decide_meanwhile(messages, info):
        if messages[0].content == "builder" and last_tool_results(messages):
            # The places node has found nothing decided, and waits again, by now.
            await asyncio.sleep(0.5)
            api.post(f"/api/schema-changes/{places_request['id']}/reject", json={})
            # That decision's task, run now: it finds the run running, and does nothing.
            task, args = queued.pop()
            thread = threading.Thread(target=task.run, args=args)
            thread.start()
            thread.join()
        async for item in flow(messages, info):
            yield item

    with use_model(model(decide_meanwhile)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", (out["status"], out["error"])
    assert out["output"] == "builder:built(rejected)\n\nbuilder-places:built(rejected)"


def test_a_waiting_run_shows_which_of_its_pins_are_no_longer_current(api, builder, store, queued):
    inner = chain(api, builder)
    outer = make_workflow(api, "Pipeline", [workflow_node("review", inner)])
    waiting = start(api, queued, outer, Flow())
    assert waiting["stale_pins"] == []

    api.put(f"/api/agents/{builder['id']}", json=agent_body(builder, base_prompt="builder-v2"))
    api.put(f"/api/workflows/{inner['id']}", json=workflow_body("Builders", []))
    after = make_agent(api, "Gone", base_prompt="gone")
    api.delete(f"/api/agents/{after['id']}")  # not pinned: not shown

    out = read(api, outer, waiting["id"])
    assert out["stale_pins"] == [
        {
            "path": ["review"],
            "kind": "workflow",
            "name": "Builders",
            "pinned_version": 1,
            "current_version": 2,
        },
        {
            "path": ["review", "build"],
            "kind": "agent",
            "name": "Builder",
            "pinned_version": 2,
            "current_version": 3,
        },
    ]

    assert api.delete(f"/api/agents/{builder['id']}").status_code == 204
    [_, build] = read(api, outer, waiting["id"])["stale_pins"]
    assert (build["path"], build["current_version"]) == (["review", "build"], None)
    # And the run's own workflow, at the top.
    api.put(f"/api/workflows/{outer['id']}", json=workflow_body("Pipeline", []))
    [own, *_] = read(api, outer, waiting["id"])["stale_pins"]
    assert own == {
        "path": [],
        "kind": "workflow",
        "name": "Pipeline",
        "pinned_version": 1,
        "current_version": 2,
    }


def test_a_run_shows_no_stale_pins_unless_its_waiting(api, builder, store, queued):
    workflow = chain(api, builder)
    waiting = start(api, queued, workflow, Flow())
    api.put(f"/api/agents/{builder['id']}", json=agent_body(builder, base_prompt="builder-v2"))
    decide(api, queued, Flow())
    assert read(api, workflow, waiting["id"])["stale_pins"] == []


def test_a_decided_request_no_call_waits_on_is_dealt_with_and_doesnt_requeue_the_run(
    api, builder, store, queued
):
    """E.g. one whose outcome its call was given just before a worker died: resuming finds no
    call waiting for it, and counts it given rather than queuing the run again and again."""
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    build = steps(waiting)["build"]
    [pending] = api.get("/api/schema-changes").json()
    with SessionLocal() as db:
        orphan = SchemaChangeRequest(
            data_store_id=store.id,
            ops=[{**CONTACTS, "name": "gone"}],
            sql="",
            summary="Create table data.gone",
            reason="Already answered",
            requested_by_run=build["agent_run_id"],
            tool_call_id="call_gone",
            base_revision=0,
            status=SchemaChangeStatus.rejected,
        )
        db.add(orphan)
        db.commit()
        orphan_id = orphan.id
    queued.append((worker.run_workflow_task, (waiting["id"],)))

    with use_model(model(flow)):
        run_queued(queued)

    out = read(api, workflow, waiting["id"])
    assert out["status"] == "waiting"
    assert steps(out)["build"]["waiting_on_request_ids"] == [pending["id"]]
    assert api.get(f"/api/schema-changes/{orphan_id}").json()["resumed_at"] is not None
    assert queued == []


def test_a_node_asking_twice_shows_both_requests_and_resumes_once_both_are_decided(
    api, builder, store, queued
):
    flow = Flow()

    async def ask_twice(messages, info):
        if messages[0].content == "builder" and not last_tool_results(messages):
            for table in ("contacts", "places"):
                yield ToolCall(
                    "request_schema_change",
                    {"ops": [{**CONTACTS, "name": table}], "reason": "Both"},
                )
            return
        async for item in flow(messages, info):
            yield item

    workflow = chain(api, builder)
    waiting = start(api, queued, workflow, ask_twice)

    requests = {r["summary"]: r["id"] for r in api.get("/api/schema-changes").json()}
    assert sorted(steps(waiting)["build"]["waiting_on_request_ids"]) == sorted(requests.values())

    api.post(f"/api/schema-changes/{requests['Create table data.places']}/reject", json={})
    with use_model(model(ask_twice)):
        run_queued(queued)
    out = read(api, workflow, waiting["id"])
    assert out["status"] == "waiting"
    assert steps(out)["build"]["waiting_on_request_ids"] == [requests["Create table data.contacts"]]

    api.post(f"/api/schema-changes/{requests['Create table data.contacts']}/approve")
    with use_model(model(ask_twice)):
        run_queued(queued)
    out = read(api, workflow, waiting["id"])
    assert out["status"] == "succeeded", out["error"]
    assert data_tables(store) == ["contacts"]


def test_reading_a_waiting_run_doesnt_load_its_agents_tools_and_takes_fixed_queries(
    api, builder, store, queued
):
    """The page polls a waiting run, possibly for days: its pins are checked without loading
    what running them needs, in as many queries however deep the graph and however many steps."""
    inner = chain(api, builder)
    middle = make_workflow(api, "Middle", [workflow_node("inner", inner)])
    outer = make_workflow(api, "Outer", [workflow_node("middle", middle)])
    shallow = start(api, queued, middle, Flow())
    deep = start(api, queued, outer, Flow())
    counts = []
    for workflow, run in ((middle, shallow), (outer, deep)):
        assert run["status"] == "waiting"
        with count_queries() as statements:
            read(api, workflow, run["id"])
        assert not [q for q in statements if "agent_version_tools" in q], statements
        counts.append(len(statements))
    assert counts[0] == counts[1]


def test_a_waiting_run_whose_store_cant_be_resolved_on_resume_fails(
    api, builder, store, queued, monkeypatch
):
    """Rather than stay waiting for good: nothing else would resume or end it."""
    workflow = chain(api, builder)
    flow = Flow()
    waiting = start(api, queued, workflow, flow)
    assert waiting["status"] == "waiting"

    def unavailable(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(workflows, "data_store_for", unavailable)
    decide(api, queued, flow, "reject")

    out = read(api, workflow, waiting["id"])
    assert (out["status"], out["error"]) == ("failed", "store unavailable")
    assert out["finished_at"]
