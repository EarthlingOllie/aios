import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
import test_workflows as workflow_tests
from fake_model import model, replies, use_model
from sqlalchemy import func, select, update
from test_agents import chat_body
from test_triggers import make_agent, make_trigger, schedule
from test_workflow_runs import echo

from aios import checkpoints, scheduler
from aios.api.agents import new_agent_run
from aios.cron import next_run
from aios.db import SessionLocal
from aios.models import (
    Agent,
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
    # Refused exactly as starting a run by hand is.
    by_hand = api.post(f"/api/agents/{agent['id']}/runs")
    assert by_hand.status_code == 409
    assert fired.last_error == by_hand.json()["detail"]


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


def test_a_process_fires_at_most_its_cap_and_leaves_the_rest_unclaimed(api, db, monkeypatch):
    from aios.config import get_settings

    monkeypatch.setattr(get_settings(), "scheduler_max_concurrent_runs", 2)
    agent = make_agent(api, "Bot")
    now = datetime.now(UTC)
    ids = []
    for i in range(3):
        t = make_trigger(api, schedule("agent", agent["id"], input=f"job {i}"))
        make_due(db, t["id"], now - timedelta(seconds=3 - i))
        ids.append(t["id"])

    async def go():
        started = []
        release = asyncio.Event()

        async def script(messages, info):
            started.append(messages[-1].content)
            await release.wait()
            yield "done"

        with use_model(model(script)):
            first = await scheduler.tick(now)
            # Only as many as there are free slots are claimed; the rest stay due, for another
            # replica or a later tick.
            assert sorted(first) == ids[:2]
            assert load(db, ids[2]).next_run_at < now
            while len(started) < 2:
                await asyncio.sleep(0.01)
            # Saturated: a tick claims nothing.
            assert await scheduler.tick(now) == {}
            release.set()
            await asyncio.gather(*first.values())
            # Slots free again: the one left over fires.
            later = await scheduler.tick(now)
            assert sorted(later) == ids[2:]
            await asyncio.gather(*later.values())
        return started

    assert sorted(asyncio.run(go())) == ["job 0", "job 1", "job 2"]
    for t_id in ids:
        assert load(db, t_id).last_status == TriggerStatus.succeeded


def age(db, trigger_id: int) -> None:
    """Make the trigger's heartbeat older than a live process would let it get."""
    db.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id)
        .values(heartbeat_at=func.now() - scheduler.STALE_AFTER - timedelta(seconds=1))
    )
    db.commit()


def test_a_claim_whose_process_died_is_failed_once_its_heartbeat_stops(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    # Claimed by a replica that is then killed before it fires (or finishes) the run.
    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == [t["id"]]
    next_run_at = load(db, t["id"]).next_run_at

    # Recently claimed, so it may still be going: the next tick, on any replica, leaves it be.
    assert tick(now) == []
    assert load(db, t["id"]).last_status == TriggerStatus.running

    # Nothing has kept it alive: it's failed, and stays scheduled.
    age(db, t["id"])
    assert tick(now) == []
    stopped = load(db, t["id"])
    assert stopped.last_status == TriggerStatus.failed
    assert stopped.last_error == scheduler.STOPPED
    assert stopped.enabled and stopped.next_run_at == next_run_at


def test_a_tick_cancelled_while_claiming_doesnt_leave_it_running(api, db, monkeypatch):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    claiming, cancelled, claimed = threading.Event(), threading.Event(), threading.Event()
    claim_due = scheduler.claim_due

    def slow_claim(db, now, limit=scheduler.BATCH):
        claiming.set()
        cancelled.wait(5)
        try:
            return claim_due(db, now, limit)
        finally:
            claimed.set()

    monkeypatch.setattr(scheduler, "claim_due", slow_claim)

    async def go():
        # Shutdown cancels the tick mid-claim; the claim's thread carries on and commits, but
        # nothing fires what it claimed.
        task = asyncio.create_task(scheduler.tick(now))
        await asyncio.to_thread(claiming.wait, 5)
        task.cancel()
        cancelled.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(claimed.wait, 5)
        await asyncio.sleep(0.1)  # anything the tick would have fired gets going

    asyncio.run(go())
    monkeypatch.setattr(scheduler, "claim_due", claim_due)
    assert load(db, t["id"]).last_status == TriggerStatus.running
    assert db.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []

    age(db, t["id"])
    tick(now)
    assert load(db, t["id"]).last_status == TriggerStatus.failed


def test_a_fire_in_flight_keeps_its_heartbeat_so_other_replicas_leave_it(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    release = None

    async def script(messages, info):
        await release.wait()
        yield "done"

    def other_replica_sweeps():
        with SessionLocal() as s:
            scheduler.fail_stalled(s)

    async def go():
        nonlocal release
        release = asyncio.Event()
        fired = await scheduler.tick(now)
        await asyncio.sleep(0.2)  # mid-turn
        # Long enough without a beat that it would count as stopped; this process beats it.
        await asyncio.to_thread(age, db, t["id"])
        await scheduler.beat()
        await asyncio.to_thread(other_replica_sweeps)
        assert (await asyncio.to_thread(load, db, t["id"])).last_status == TriggerStatus.running
        release.set()
        await asyncio.gather(*fired.values())

    with use_model(model(script)):
        asyncio.run(go())
    assert load(db, t["id"]).last_status == TriggerStatus.succeeded


def test_the_loop_beats_the_fires_in_flight(api, db, monkeypatch):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with SessionLocal() as s:
        scheduler.claim_due(s, now)
    age(db, t["id"])

    async def fake_tick(now=None):
        return {}

    monkeypatch.setattr(scheduler, "tick", fake_tick)
    monkeypatch.setattr(scheduler, "HEARTBEAT_SECONDS", 0.01)

    async def go():
        # A fire of this trigger is in flight in this process.
        in_flight = asyncio.create_task(asyncio.Event().wait())
        scheduler._firing[in_flight] = (t["id"], now)
        loop = asyncio.create_task(scheduler.run_forever(interval=10))
        await asyncio.sleep(0.3)
        loop.cancel()
        await asyncio.gather(loop, in_flight, return_exceptions=True)

    asyncio.run(go())
    scheduler._firing.clear()
    with SessionLocal() as s:
        scheduler.fail_stalled(s)
    assert load(db, t["id"]).last_status == TriggerStatus.running


def test_a_schedule_retargeted_as_it_fires_isnt_left_running(api, db, queued, monkeypatch):
    agent = make_agent(api, "Bot")
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    target = scheduler._target

    def retargeted_meanwhile(trigger_id, fired_at):
        kind = target(trigger_id, fired_at)
        resp = api.put(f"/api/triggers/{t['id']}", json=schedule("workflow", workflow["id"]))
        assert resp.status_code == 200, resp.text
        return kind

    monkeypatch.setattr(scheduler, "_target", retargeted_meanwhile)
    with use_model(replies("hi")):
        assert tick(now) == [t["id"]]
    # Neither target runs this time (the new one does next time), and it isn't left running:
    # the edit dropped the firing, as if it never happened.
    assert queued == []
    assert db.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []
    fired = load(db, t["id"])
    assert (fired.last_fired_at, fired.last_status, fired.last_error) == (None, None, None)


def test_a_saturated_process_still_fails_stopped_schedules(api, db, monkeypatch):
    # The cap stops a busy process claiming, not sweeping: with one replica, nothing else would.
    from aios.config import get_settings

    monkeypatch.setattr(get_settings(), "scheduler_max_concurrent_runs", 1)
    agent = make_agent(api, "Bot")
    stopped = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, stopped["id"], now - timedelta(seconds=1))
    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == [stopped["id"]]  # by a replica since killed
    age(db, stopped["id"])

    async def go():
        busy = asyncio.create_task(asyncio.Event().wait())
        scheduler._firing[busy] = (0, now)  # this process's one slot is taken
        try:
            assert await scheduler.tick(now) == {}
        finally:
            busy.cancel()
            await asyncio.gather(busy, return_exceptions=True)
            scheduler._firing.clear()

    asyncio.run(go())
    assert load(db, stopped["id"]).last_status == TriggerStatus.failed


def claim(db, trigger_id: int, now: datetime) -> None:
    make_due(db, trigger_id, now - timedelta(seconds=1))
    with SessionLocal() as s:
        assert scheduler.claim_due(s, now) == [trigger_id]


def put(api, trigger_id: int, body: dict) -> dict:
    resp = api.put(f"/api/triggers/{trigger_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def agent_runs(db, agent_id: int) -> list[AgentRun]:
    db.expire_all()
    return db.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id)).all()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param({"enabled": False}, id="paused"),
        pytest.param({"input": "Something else"}, id="message-changed"),
        pytest.param({"cron": "*/5 * * * *"}, id="rescheduled"),
    ],
)
def test_editing_an_agent_schedule_after_its_claimed_cancels_that_firing(api, db, edit):
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    # Edited between being claimed and starting its run: that firing was the old schedule's,
    # so it's dropped, and shown as if it never happened.
    shown = put(api, t["id"], {**body, **edit})
    assert (shown["last_fired_at"], shown["last_status"]) == (None, None)

    asyncio.run(scheduler.fire(t["id"], now))

    assert agent_runs(db, agent["id"]) == []
    after = api.get(f"/api/triggers/{t['id']}").json()
    assert [after[k] for k in ("last_fired_at", "last_status", "last_run_id")] == [None] * 3
    assert after["enabled"] == edit.get("enabled", True)


def test_retargeting_an_agent_schedule_after_its_claimed_starts_neither_agent(api, db):
    old, new = make_agent(api, "Old"), make_agent(api, "New")
    body = schedule("agent", old["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    put(api, t["id"], {**body, "target_id": new["id"]})

    asyncio.run(scheduler.fire(t["id"], now))

    assert agent_runs(db, old["id"]) == [] and agent_runs(db, new["id"]) == []
    assert load(db, t["id"]).last_status is None


def test_pausing_a_workflow_schedule_after_its_claimed_cancels_that_firing(api, db, queued):
    workflow = make_runnable_workflow(api)
    body = schedule("workflow", workflow["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    put(api, t["id"], {**body, "enabled": False})

    asyncio.run(scheduler.fire(t["id"], now))

    assert queued == []
    assert (
        db.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"])).all() == []
    )
    paused = load(db, t["id"])
    assert (paused.last_fired_at, paused.last_status, paused.enabled) == (None, None, False)


def test_a_pause_landing_as_the_run_is_being_created_drops_it(api, db, monkeypatch):
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)

    def pause_then_create(db, agent, **kwargs):
        # Paused after the scheduler checked the trigger, before it's locked for the run.
        put(api, t["id"], {**body, "enabled": False})
        return new_agent_run(db, agent, **kwargs)

    monkeypatch.setattr(scheduler.agents_api, "new_agent_run", pause_then_create)
    asyncio.run(scheduler.fire(t["id"], now))

    # The run it had added is rolled back with the firing.
    assert agent_runs(db, agent["id"]) == []
    paused = load(db, t["id"])
    assert (paused.last_fired_at, paused.last_status, paused.last_run_id) == (None, None, None)


def test_pausing_a_schedule_once_its_run_has_started_lets_the_run_finish(api, db):
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    async def script(messages, info):
        await asyncio.to_thread(put, api, t["id"], {**body, "enabled": False})
        yield "done"

    with use_model(model(script)):
        tick(now)
    (run,) = agent_runs(db, agent["id"])
    fired = load(db, t["id"])
    # Too late to cancel: the run it started is kept, and shown with how it went.
    assert (fired.last_fired_at, fired.last_status, fired.last_run_id) == (
        now,
        TriggerStatus.succeeded,
        run.id,
    )
    assert run.message_count == 2
    assert not fired.enabled and fired.next_run_at is None


def test_a_pause_waiting_on_a_run_being_started_doesnt_cancel_it(api, db):
    """A pause racing the start of a run waits for it, then sees that it has started."""
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)

    async def go():
        with SessionLocal() as s:
            # Mid-start, as the scheduler starts a run: the agent share-locked, then the
            # trigger row locked, until the run and the link to it are committed together.
            run = new_agent_run(s, s.get(Agent, agent["id"]), trigger_id=t["id"])
            trigger = s.scalars(
                select(Trigger).where(Trigger.id == t["id"]).with_for_update()
            ).one()
            pausing = asyncio.create_task(
                asyncio.to_thread(put, api, t["id"], {**body, "enabled": False})
            )
            await asyncio.sleep(0.3)
            assert not pausing.done()
            trigger.last_run_id = run.id
            s.commit()
            run_id = run.id
        return await pausing, run_id

    shown, run_id = asyncio.run(go())
    assert shown["enabled"] is False
    assert (shown["last_status"], shown["last_run_id"]) == ("running", run_id)
    assert load(db, t["id"]).last_fired_at == now
