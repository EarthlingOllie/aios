import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import test_workflows as workflow_tests
from fake_model import model, replies, use_model
from sqlalchemy import select, update
from test_agents import chat_body
from test_triggers import make_agent, make_trigger, schedule
from test_workflow_runs import echo

from aios import checkpoints, scheduler
from aios.cron import next_run
from aios.db import SessionLocal
from aios.models import (
    AgentRun,
    AgentVersionTool,
    Trigger,
    TriggerStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from aios.worker import run_workflow_task
from aios.workflows import execute_run


def make_due(db, trigger_id: int, at: datetime) -> None:
    db.execute(update(Trigger).where(Trigger.id == trigger_id).values(next_run_at=at))
    db.commit()


def load(db, trigger_id: int) -> Trigger:
    db.expire_all()
    return db.get(Trigger, trigger_id)


def tick(now: datetime) -> list[int]:
    """Runs one scheduler tick and waits for everything it fired."""

    async def go():
        fired = await scheduler.tick(now)
        await asyncio.gather(*fired.values())
        return sorted(fired)

    return asyncio.run(go())


def test_claim_due_advances_only_due_enabled_schedules(api, db):
    agent = make_agent(api, "Bot")
    now = datetime(2030, 1, 1, 12, 0, 30, tzinfo=UTC)
    due = make_trigger(api, schedule("agent", agent["id"], cron="*/5 * * * *", timezone="UTC"))
    not_yet = make_trigger(api, schedule("agent", agent["id"], cron="*/5 * * * *"))
    off = make_trigger(api, schedule("agent", agent["id"], enabled=False))
    # Missed several runs (the service was down): it fires once, and moves on from now.
    make_due(db, due["id"], now - timedelta(hours=3))
    make_due(db, not_yet["id"], now + timedelta(minutes=1))

    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == [due["id"]]
    t = load(db, due["id"])
    assert t.next_run_at == datetime(2030, 1, 1, 12, 5, tzinfo=UTC)
    assert t.last_fired_at == now
    assert t.last_status == TriggerStatus.running
    assert load(db, not_yet["id"]).next_run_at == now + timedelta(minutes=1)
    assert load(db, off["id"]).next_run_at is None
    # Claimed once: it isn't due again until its next run.
    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == []


def test_a_schedule_locked_by_another_replica_is_skipped(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with SessionLocal() as other:
        # Another replica is mid-claim.
        other.execute(select(Trigger).where(Trigger.id == t["id"]).with_for_update())
        with SessionLocal() as s:
            assert scheduler.claim_due(s, now) == []
        other.rollback()
    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == [t["id"]]


def test_firing_an_agent_schedule_starts_a_run_with_its_message(api, db):
    agent = make_agent(api, "Newsbot")
    t = make_trigger(api, schedule("agent", agent["id"], input="What happened today?"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    seen = []

    async def script(messages, info):
        seen.append(messages[-1].content)
        yield "Not much."

    with use_model(model(script)):
        assert tick(now) == [t["id"]]

    assert seen == ["What happened today?"]
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.succeeded
    assert fired.last_error is None
    assert fired.next_run_at == next_run("0 9 * * *", "Europe/London", now)
    run = db.get(AgentRun, fired.last_run_id)
    assert run.agent_id == agent["id"]
    assert run.agent_version_id == agent["current_version"]["id"]
    assert run.trigger_id == t["id"]
    assert run.title == "What happened today?"
    assert run.message_count == 2
    history = checkpoints.history(run.id)
    assert [m.type for m in history] == ["human", "ai"]
    assert history[1].text == "Not much."

    # The run shows up among the agent's runs, marked as started by the schedule.
    runs = api.get(f"/api/agents/{agent['id']}/runs").json()
    assert [(r["id"], r["trigger_id"]) for r in runs] == [(run.id, t["id"])]


def test_a_failed_turn_is_recorded_on_the_schedule(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    async def script(messages, info):
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(script)):
        tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert fired.last_error == "model overloaded"
    # The run is kept, with what the agent got to, like a failed chat turn.
    run = db.get(AgentRun, fired.last_run_id)
    assert [m.type for m in checkpoints.history(run.id)] == ["human", "ai"]
    # It's still scheduled.
    assert fired.enabled and fired.next_run_at > now


def test_an_agent_that_cant_run_fails_without_a_run(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    db.add(
        AgentVersionTool(
            agent_version_id=agent["current_version"]["id"],
            tool_key="retired_tool",
            config={},
            position=0,
        )
    )
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with use_model(replies("hi")):
        tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert "retired_tool" in fired.last_error
    assert fired.last_run_id is None
    assert db.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    """The ids of the workflow runs started, instead of sending them to the worker."""
    ids = []
    monkeypatch.setattr(run_workflow_task, "delay", ids.append)
    return ids


def make_runnable_workflow(api, name: str = "Flow") -> dict:
    """A workflow of one agent node, whose agent answers `echo(<input>)` (see `echo`)."""
    agent = workflow_tests.make_agent(api, f"{name} agent", base_prompt="echo")
    return workflow_tests.make_workflow(api, name, [workflow_tests.agent_node("a", agent)])


def test_firing_a_workflow_schedule_starts_a_run_with_its_input(api, db, queued):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"], input="Today's tickets"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    assert tick(now) == [t["id"]]

    fired = load(db, t["id"])
    assert fired.last_error is None and fired.last_run_id is None
    run = db.get(WorkflowRun, fired.last_workflow_run_id)
    # Started as `POST /api/workflows/{id}/runs` starts one: on the current version, queued
    # for the worker.
    assert run.workflow_id == workflow["id"]
    assert run.workflow_version_id == workflow["current_version"]["id"]
    assert run.input == "Today's tickets"
    assert run.status == WorkflowRunStatus.queued
    assert queued == [run.id]
    assert fired.next_run_at == next_run("0 9 * * *", "Europe/London", now)

    # The worker runs it; the schedule shows how the run is going, and then how it went.
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert (shown["last_status"], shown["last_workflow_run_id"]) == ("running", run.id)
    with use_model(echo()):
        asyncio.run(execute_run(run.id))
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert (shown["last_status"], shown["last_error"]) == ("succeeded", None)
    out = api.get(f"/api/workflows/{workflow['id']}/runs/{run.id}").json()
    assert out["output"] == "echo(Today's tickets)"


def test_a_failed_workflow_run_shows_on_its_schedule(api, db, queued):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    with use_model(echo(fail={"echo"})):
        asyncio.run(execute_run(queued[-1]))
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert shown["last_status"] == "failed"
    assert "echo broke" in shown["last_error"]
    assert shown["last_workflow_run_id"] == queued[-1]


def test_a_workflow_that_cant_run_fails_without_a_run(api, db, queued):
    # Refused as starting it by hand would be: here, it has no nodes.
    workflow = workflow_tests.make_workflow(api, "Empty")
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert fired.last_error == "This workflow has no nodes"
    assert fired.last_workflow_run_id is None
    assert queued == []
    assert (
        db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"])).all() == []
    )
    assert fired.enabled and fired.next_run_at > now


def test_a_workflow_run_that_cant_be_queued_fails_without_a_run(api, db, monkeypatch):
    def broker_down(run_id):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(run_workflow_task, "delay", broker_down)
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert "Could not queue the run" in fired.last_error
    assert fired.last_workflow_run_id is None
    runs = db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"]))
    assert runs.all() == []


def test_a_later_firing_that_fails_isnt_shown_as_the_earlier_run(api, db, queued):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    with use_model(echo()):
        asyncio.run(execute_run(queued[-1]))
    assert api.get(f"/api/triggers/{t['id']}").json()["last_status"] == "succeeded"

    # Its current version now can't run.
    resp = api.put(f"/api/workflows/{workflow['id']}", json=workflow_tests.workflow_body("Flow"))
    assert resp.status_code == 200, resp.text
    later = now + timedelta(days=1)
    make_due(db, t["id"], later - timedelta(seconds=1))
    tick(later)
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert (shown["last_status"], shown["last_error"]) == ("failed", "This workflow has no nodes")
    assert shown["last_workflow_run_id"] is None


def test_a_trigger_deleted_mid_turn_doesnt_break_the_run(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    async def script(messages, info):
        await asyncio.to_thread(lambda: api.delete(f"/api/triggers/{t['id']}"))
        yield "done"

    with use_model(model(script)):
        tick(now)
    assert load(db, t["id"]) is None
    (run,) = db.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all()
    assert run.trigger_id is None
    assert run.message_count == 2


def test_the_loop_runs_ticks_until_cancelled(monkeypatch):
    ticks = []

    async def fake_tick(now=None):
        ticks.append(now)
        return {}

    monkeypatch.setattr(scheduler, "tick", fake_tick)

    async def go():
        task = asyncio.create_task(scheduler.run_forever(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert len(ticks) >= 2


def test_the_api_runs_the_scheduler_while_its_up(monkeypatch):
    from fastapi.testclient import TestClient

    from aios.config import get_settings
    from aios.main import app

    state = {"started": 0, "stopped": 0}

    async def fake_run_forever(interval):
        state["started"] += 1
        try:
            await asyncio.Event().wait()
        finally:
            state["stopped"] += 1

    monkeypatch.setattr(scheduler, "run_forever", fake_run_forever)
    with TestClient(app):
        pass
    assert state == {"started": 1, "stopped": 1}

    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    with TestClient(app):
        pass
    assert state == {"started": 1, "stopped": 1}


def test_a_scheduled_run_is_stored_and_continued_like_a_chat_run(api, db):
    agent = make_agent(api, "Newsbot")
    runs_url = f"/api/agents/{agent['id']}/runs"
    t = make_trigger(api, schedule("agent", agent["id"], input="What happened today?"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    async def script(messages, info):
        yield "Not much."

    with use_model(model(script)):
        tick(now)
        scheduled = load(db, t["id"]).last_run_id
        chatted = api.post(runs_url).json()["id"]
        api.post(f"{runs_url}/{chatted}/chat", json=chat_body("What happened today?"))
        # The user can continue a scheduled run from the UI, as any other.
        for run_id in (scheduled, chatted):
            assert api.post(f"{runs_url}/{run_id}/chat", json=chat_body("More?")).status_code == 200

    def stored(run_id):
        detail = api.get(f"{runs_url}/{run_id}").json()
        ids = [m["id"] for m in detail["ui_messages"]]
        # Each message has an id of its own, kept in the run's thread, so the UI's are stable.
        assert ids == [m.id for m in checkpoints.history(run_id)]
        assert all(ids) and len(set(ids)) == len(ids)
        assert ids == [m["id"] for m in api.get(f"{runs_url}/{run_id}").json()["ui_messages"]]
        return detail["message_count"], [(m["role"], m["parts"]) for m in detail["ui_messages"]]

    assert stored(scheduled) == stored(chatted)
    assert stored(scheduled)[0] == 4
