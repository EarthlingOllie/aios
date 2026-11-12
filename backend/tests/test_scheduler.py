import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
import test_workflows as workflow_tests
from conftest import (
    QueuedRuns,
    capture_runs,
    drain_run,
    home_organisation,
    signed_in_store,
    take_turn,
    user_store,
)
from sqlalchemy import func, select, update
from test_triggers import make_agent, make_trigger, schedule
from test_workflow_runs import echo

from aios import checkpoints, scheduler, schedules
from aios.api import workflows as workflows_api
from aios.api.agents import new_agent_run
from aios.cron import next_run
from aios.datastores import store_session
from aios.datastores.models import Agent, AgentRun, AgentVersionTool, Trigger, WorkflowRun
from aios.db import SessionLocal
from aios.models import ScheduleDue, TriggerStatus, WorkflowRunStatus
from aios.run_service import executor
from aios.testing.fake_model import model, replies, use_model


def make_due(db, trigger_id: int, at: datetime) -> None:
    """Make the signed-in user's trigger due at `at`, in its store and in the due index, as
    saving it then would."""
    store_id = signed_in_store()
    with store_session(store_id) as store:
        store.execute(update(Trigger).where(Trigger.id == trigger_id).values(next_run_at=at))
        store.commit()
    schedules.index(store_id, [(trigger_id, at)])


def change(trigger_id: int, **values) -> None:
    """Change the signed-in user's trigger behind the API's back."""
    with store_session(signed_in_store()) as store:
        store.execute(update(Trigger).where(Trigger.id == trigger_id).values(**values))
        store.commit()


def load(db, trigger_id: int) -> Trigger | None:
    with store_session(signed_in_store()) as store:
        return store.get(Trigger, trigger_id)


def ids(claimed) -> list[int]:
    """The trigger ids of what `claim_due` or `tick` claimed, in order."""
    return [trigger_id for _, trigger_id in claimed]


def claimed_ids(db, now: datetime) -> list[int]:
    return ids(scheduler.claim_due(db, now))


def sweep_stalled() -> None:
    """Fail the signed-in user's store's stalled schedules, as any replica's tick does."""
    with store_session(signed_in_store()) as store:
        scheduler.fail_stalled(store)


def drain() -> None:
    """Run what's queued on the run service, as its processes would."""
    asyncio.run(executor.drain_once())


def fire(trigger_id: int, fired_at: datetime) -> None:
    """Fire the claimed trigger, and run the turn it enqueued."""
    asyncio.run(scheduler.fire(signed_in_store(), trigger_id, fired_at))
    drain()


def tick(now: datetime) -> list[int]:
    """Runs one scheduler tick, and the run service runs everything it enqueued; the trigger
    ids it claimed."""
    claimed = asyncio.run(scheduler.tick(now))
    drain()
    return sorted(ids(claimed))


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
        assert claimed_ids(s, now) == [due["id"]]
    t = load(db, due["id"])
    assert t.next_run_at == datetime(2030, 1, 1, 12, 5, tzinfo=UTC)
    assert t.last_fired_at == now
    assert t.last_status == TriggerStatus.running
    assert load(db, not_yet["id"]).next_run_at == now + timedelta(minutes=1)
    assert load(db, off["id"]).next_run_at is None
    # Claimed once: it isn't due again until its next run.
    with SessionLocal() as s:
        assert claimed_ids(s, now) == []


def test_a_schedule_locked_by_another_replica_is_skipped(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with SessionLocal() as other:
        # Another replica is mid-claim.
        other.execute(
            select(ScheduleDue).where(ScheduleDue.trigger_id == t["id"]).with_for_update()
        )
        with SessionLocal() as s:
            assert claimed_ids(s, now) == []
        other.rollback()
    with SessionLocal() as s:
        assert claimed_ids(s, now) == [t["id"]]


def test_firing_an_agent_schedule_starts_a_run_with_its_message(api, db, in_store):
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
    run = in_store.get(AgentRun, fired.last_run_id)
    assert run.agent_id == agent["id"]
    assert run.agent_version_id == agent["current_version"]["id"]
    assert run.trigger_id == t["id"]
    assert run.title == "What happened today?"
    assert run.message_count == 2
    history = checkpoints.history(in_store.store_id, run.id)
    assert [m.type for m in history] == ["human", "ai"]
    assert history[1].text == "Not much."

    # The run shows up among the agent's runs, marked as started by the schedule.
    runs = api.get(f"/api/agents/{agent['id']}/runs").json()
    assert [(r["id"], r["trigger_id"]) for r in runs] == [(run.id, t["id"])]


def test_a_failed_turn_is_recorded_on_the_schedule(api, db, in_store):
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
    run = in_store.get(AgentRun, fired.last_run_id)
    assert [m.type for m in checkpoints.history(in_store.store_id, run.id)] == ["human", "ai"]
    # It's still scheduled.
    assert fired.enabled and fired.next_run_at > now


def test_an_agent_that_cant_run_fails_without_a_run(api, db, user, in_store):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    with user_store(user) as store:
        store.add(
            AgentVersionTool(
                agent_version_id=agent["current_version"]["id"],
                tool_key="retired_tool",
                config={},
                position=0,
            )
        )
        store.commit()
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    with use_model(replies("hi")):
        tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert "retired_tool" in fired.last_error
    assert fired.last_run_id is None
    assert in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []
    # Refused exactly as starting a run by hand is.
    by_hand = api.post(f"/api/agents/{agent['id']}/runs")
    assert by_hand.status_code == 409
    assert fired.last_error == by_hand.json()["detail"]


@pytest.fixture
def queued(monkeypatch) -> QueuedRuns:
    """The workflow runs started, for the test to run (`drain_run`)."""
    return capture_runs(monkeypatch, None)


def make_runnable_workflow(api, name: str = "Flow") -> dict:
    """A workflow of one agent node, whose agent answers `echo(<input>)` (see `echo`)."""
    agent = workflow_tests.make_agent(api, f"{name} agent", base_prompt="echo")
    return workflow_tests.make_workflow(api, name, [workflow_tests.agent_node("a", agent)])


def test_firing_a_workflow_schedule_starts_a_run_with_its_input(api, db, queued, in_store):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"], input="Today's tickets"))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))

    assert tick(now) == [t["id"]]

    fired = load(db, t["id"])
    assert fired.last_error is None and fired.last_run_id is None
    run = in_store.get(WorkflowRun, fired.last_workflow_run_id)
    # Started as `POST /api/workflows/{id}/runs` starts one: on the current version, queued
    # for the run service.
    assert run.workflow_id == workflow["id"]
    assert run.workflow_version_id == workflow["current_version"]["id"]
    assert run.input == "Today's tickets"
    assert run.status == WorkflowRunStatus.queued
    assert queued == [run.id]
    assert fired.next_run_at == next_run("0 9 * * *", "Europe/London", now)

    # The run service runs it; the schedule shows how the run is going, and then how it went.
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert (shown["last_status"], shown["last_workflow_run_id"]) == ("running", run.id)
    with use_model(echo()):
        drain_run(in_store.store_id, run.id)
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
        drain_run(queued.store, queued[-1])
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert shown["last_status"] == "failed"
    assert "echo broke" in shown["last_error"]
    assert shown["last_workflow_run_id"] == queued[-1]


def test_a_workflow_run_waiting_for_a_decision_shows_its_schedule_running(
    api, db, queued, in_store
):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    in_store.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == queued[-1])
        .values(status=WorkflowRunStatus.waiting, started_at=func.now())
    )
    db.commit()

    resp = api.get(f"/api/triggers/{t['id']}")
    assert resp.status_code == 200, resp.text
    assert (resp.json()["last_status"], resp.json()["last_error"]) == ("running", None)
    assert api.get("/api/triggers").status_code == 200


def test_a_workflow_that_cant_run_fails_without_a_run(api, db, queued, in_store):
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
        in_store.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"])).all()
        == []
    )
    assert fired.enabled and fired.next_run_at > now


def test_a_workflow_run_that_cant_be_queued_fails_without_a_run(api, db, monkeypatch, in_store):
    def queue_down(work):
        raise ConnectionError("the platform database is down")

    monkeypatch.setattr(workflows_api, "enqueue_run", queue_down)
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    fired = load(db, t["id"])
    assert fired.last_status == TriggerStatus.failed
    assert "Could not queue the run" in fired.last_error
    assert fired.last_workflow_run_id is None
    runs = in_store.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"]))
    assert runs.all() == []


def test_a_later_firing_that_fails_isnt_shown_as_the_earlier_run(api, db, queued):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    tick(now)
    with use_model(echo()):
        drain_run(queued.store, queued[-1])
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


def test_a_trigger_deleted_mid_turn_doesnt_break_the_run(api, db, in_store):
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
    (run,) = in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all()
    assert run.trigger_id is None
    assert run.message_count == 2


def run_service_for(seconds: float) -> None:
    """Run a run service process for a moment, then stop it."""

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(executor.run_forever(stopping))
        await asyncio.sleep(seconds)
        stopping.set()
        await task

    asyncio.run(go())


def test_the_run_service_ticks_the_scheduler_unless_its_turned_off(monkeypatch):
    from aios.config import get_settings

    ticks = []

    async def fake_tick(now=None):
        ticks.append(now)
        return []

    monkeypatch.setattr(scheduler, "tick", fake_tick)
    monkeypatch.setattr(get_settings(), "scheduler_interval_seconds", 0.01)
    run_service_for(0.2)
    assert len(ticks) >= 2

    ticks.clear()
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    run_service_for(0.1)
    assert ticks == []


def test_the_api_runs_no_schedules(monkeypatch):
    from fastapi.testclient import TestClient

    from aios.main import app

    ticks = []

    async def fake_tick(now=None):
        ticks.append(now)
        return []

    monkeypatch.setattr(scheduler, "tick", fake_tick)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert ticks == []


def test_a_scheduled_run_is_stored_and_continued_like_a_chat_run(api, db, in_store):
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
        take_turn(api, agent["id"], chatted, "What happened today?")
        # A scheduled run takes another turn as any other.
        for run_id in (scheduled, chatted):
            take_turn(api, agent["id"], run_id, "More?")

    def stored(run_id):
        detail = api.get(f"{runs_url}/{run_id}").json()
        ids = [m["id"] for m in detail["ui_messages"]]
        # Each message has an id of its own, kept in the run's thread, so the UI's are stable.
        assert ids == [m.id for m in checkpoints.history(in_store.store_id, run_id)]
        assert all(ids) and len(set(ids)) == len(ids)
        assert ids == [m["id"] for m in api.get(f"{runs_url}/{run_id}").json()["ui_messages"]]
        return detail["message_count"], [(m["role"], m["parts"]) for m in detail["ui_messages"]]

    assert stored(scheduled) == stored(chatted)
    assert stored(scheduled)[0] == 4


def age(db, trigger_id: int) -> None:
    """Make the trigger's claim older than a live process would let it get."""
    with store_session(signed_in_store()) as store:
        store.execute(
            update(Trigger)
            .where(Trigger.id == trigger_id)
            .values(claimed_at=func.now() - scheduler.STALE_AFTER - timedelta(seconds=1))
        )
        store.commit()


def test_a_claim_whose_process_died_is_failed_once_it_goes_stale(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    # Claimed by a replica that is then killed before it fires (or finishes) the run.
    with SessionLocal() as s:
        assert claimed_ids(s, now) == [t["id"]]
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


def test_a_tick_cancelled_while_claiming_doesnt_leave_it_running(api, db, monkeypatch, in_store):
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
    assert in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []

    age(db, t["id"])
    tick(now)
    assert load(db, t["id"]).last_status == TriggerStatus.failed


def test_a_schedule_whose_run_has_started_isnt_failed_however_long_its_turn(api, db):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    seen = []

    async def script(messages, info):
        # Long past when a claim that never started would count as stopped: the run service has
        # its turn, and any process's sweep leaves it be.
        await asyncio.to_thread(age, db, t["id"])
        await asyncio.to_thread(sweep_stalled)
        seen.append((await asyncio.to_thread(load, db, t["id"])).last_status)
        yield "done"

    with use_model(model(script)):
        assert tick(now) == [t["id"]]
    assert seen == [TriggerStatus.running]
    assert load(db, t["id"]).last_status == TriggerStatus.succeeded


def test_a_schedule_retargeted_as_it_fires_isnt_left_running(
    api, db, queued, monkeypatch, in_store
):
    agent = make_agent(api, "Bot")
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    claimed = scheduler._claimed

    def retargeted_meanwhile(store, trigger_id, fired_at, kind=None, *, lock=False):
        trigger = claimed(store, trigger_id, fired_at, kind, lock=lock)
        if not lock:
            # Read as the agent's schedule, then retargeted before its run starts.
            resp = api.put(f"/api/triggers/{t['id']}", json=schedule("workflow", workflow["id"]))
            assert resp.status_code == 200, resp.text
        return trigger

    monkeypatch.setattr(scheduler, "_claimed", retargeted_meanwhile)
    with use_model(replies("hi")):
        assert tick(now) == [t["id"]]
    # Neither target runs this time (the new one does next time), and it isn't left running:
    # the edit dropped the firing, as if it never happened.
    assert queued == []
    assert in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all() == []
    fired = load(db, t["id"])
    assert (fired.last_fired_at, fired.last_status, fired.last_error) == (None, None, None)


def claim(db, trigger_id: int, now: datetime) -> None:
    make_due(db, trigger_id, now - timedelta(seconds=1))
    with SessionLocal() as s:
        assert claimed_ids(s, now) == [trigger_id]


def put(api, trigger_id: int, body: dict) -> dict:
    resp = api.put(f"/api/triggers/{trigger_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def agent_runs(in_store, agent_id: int) -> list[AgentRun]:
    return in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id)).all()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param({"enabled": False}, id="paused"),
        pytest.param({"input": "Something else"}, id="message-changed"),
        pytest.param({"cron": "*/5 * * * *"}, id="rescheduled"),
    ],
)
def test_editing_an_agent_schedule_after_its_claimed_cancels_that_firing(api, db, edit, in_store):
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    # Edited between being claimed and starting its run: that firing was the old schedule's,
    # so it's dropped, and shown as if it never happened.
    shown = put(api, t["id"], {**body, **edit})
    assert (shown["last_fired_at"], shown["last_status"]) == (None, None)

    fire(t["id"], now)

    assert agent_runs(in_store, agent["id"]) == []
    after = api.get(f"/api/triggers/{t['id']}").json()
    assert [after[k] for k in ("last_fired_at", "last_status", "last_run_id")] == [None] * 3
    assert after["enabled"] == edit.get("enabled", True)


def test_retargeting_an_agent_schedule_after_its_claimed_starts_neither_agent(api, db, in_store):
    old, new = make_agent(api, "Old"), make_agent(api, "New")
    body = schedule("agent", old["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    put(api, t["id"], {**body, "target_id": new["id"]})

    fire(t["id"], now)

    assert agent_runs(in_store, old["id"]) == [] and agent_runs(in_store, new["id"]) == []
    assert load(db, t["id"]).last_status is None


def test_pausing_a_workflow_schedule_after_its_claimed_cancels_that_firing(
    api, db, queued, in_store
):
    workflow = make_runnable_workflow(api)
    body = schedule("workflow", workflow["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    put(api, t["id"], {**body, "enabled": False})

    fire(t["id"], now)

    assert queued == []
    assert (
        in_store.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow["id"])).all()
        == []
    )
    paused = load(db, t["id"])
    assert (paused.last_fired_at, paused.last_status, paused.enabled) == (None, None, False)


def test_a_pause_landing_as_the_run_is_being_created_drops_it(api, db, monkeypatch, in_store):
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)

    def pause_then_create(store, *args, **kwargs):
        # Paused after the scheduler checked the trigger, before it's locked for the run.
        put(api, t["id"], {**body, "enabled": False})
        return new_agent_run(store, *args, **kwargs)

    monkeypatch.setattr(scheduler.agents_api, "new_agent_run", pause_then_create)
    fire(t["id"], now)

    # The run it had added is rolled back with the firing.
    assert agent_runs(in_store, agent["id"]) == []
    paused = load(db, t["id"])
    assert (paused.last_fired_at, paused.last_status, paused.last_run_id) == (None, None, None)


def test_pausing_a_schedule_once_its_run_has_started_lets_the_run_finish(api, db, in_store):
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
    (run,) = agent_runs(in_store, agent["id"])
    fired = load(db, t["id"])
    # Too late to cancel: the run it started is kept, and shown with how it went.
    assert (fired.last_fired_at, fired.last_status, fired.last_run_id) == (
        now,
        TriggerStatus.succeeded,
        run.id,
    )
    assert run.message_count == 2
    assert not fired.enabled and fired.next_run_at is None


def test_a_pause_waiting_on_a_run_being_started_doesnt_cancel_it(api, db, user, in_store):
    """A pause racing the start of a run waits for it, then sees that it has started."""
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    claim(db, t["id"], now)
    org_id = home_organisation(db, user.id).id

    async def go():
        with user_store(user) as store:
            # Mid-start, as the scheduler starts a run: the agent share-locked, then the
            # trigger row locked, until the run and the link to it are committed.
            agent_row = store.get(Agent, agent["id"])
            run = new_agent_run(store, org_id, agent_row, created_by=None, trigger_id=t["id"])
            trigger = store.scalars(
                select(Trigger).where(Trigger.id == t["id"]).with_for_update()
            ).one()
            pausing = asyncio.create_task(
                asyncio.to_thread(put, api, t["id"], {**body, "enabled": False})
            )
            await asyncio.sleep(0.3)
            assert not pausing.done()
            trigger.last_run_id = run.id
            store.commit()
            run_id = run.id
        return await pausing, run_id

    shown, run_id = asyncio.run(go())
    assert shown["enabled"] is False
    assert (shown["last_status"], shown["last_run_id"]) == ("running", run_id)
    assert load(db, t["id"]).last_fired_at == now


def reclaim(trigger_id: int, now: datetime) -> list[int]:
    """Make the trigger due at `now` and claim it, in a session of its own, as another replica
    (or this one's next tick) would."""
    with SessionLocal() as s:
        make_due(s, trigger_id, now - timedelta(seconds=1))
        return claimed_ids(s, now)


def workflow_runs(in_store, workflow_id: int) -> list[WorkflowRun]:
    return in_store.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)).all()


def test_an_agent_schedule_reclaimed_as_its_run_starts_drops_the_old_firing(
    api, db, monkeypatch, in_store
):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    later = now + timedelta(minutes=1)
    claim(db, t["id"], now)

    def reclaim_then_create(store, *args, **kwargs):
        # Fired again (a tight schedule, a slow start) before this firing commits its run.
        assert reclaim(t["id"], later) == [t["id"]]
        return new_agent_run(store, *args, **kwargs)

    monkeypatch.setattr(scheduler.agents_api, "new_agent_run", reclaim_then_create)
    fire(t["id"], now)
    monkeypatch.setattr(scheduler.agents_api, "new_agent_run", new_agent_run)

    # The superseded firing's run is rolled back, and the trigger isn't pointed at it: it
    # shows the newer firing, still to start its run.
    assert agent_runs(in_store, agent["id"]) == []
    t1 = load(db, t["id"])
    assert (t1.last_fired_at, t1.last_status, t1.last_run_id) == (
        later,
        TriggerStatus.running,
        None,
    )

    # The newer firing starts its own run, and is linked to it.
    with use_model(replies("hi")):
        fire(t["id"], later)
    (run,) = agent_runs(in_store, agent["id"])
    t2 = load(db, t["id"])
    assert (t2.last_fired_at, t2.last_status, t2.last_run_id) == (
        later,
        TriggerStatus.succeeded,
        run.id,
    )


def test_a_workflow_schedule_reclaimed_as_its_run_starts_drops_the_old_firing(
    api, db, queued, monkeypatch, in_store
):
    workflow = make_runnable_workflow(api)
    t = make_trigger(api, schedule("workflow", workflow["id"]))
    now = datetime.now(UTC)
    later = now + timedelta(minutes=1)
    claim(db, t["id"], now)
    new_workflow_run = scheduler.workflows_api.new_workflow_run

    def reclaim_then_create(store, *args, **kwargs):
        assert reclaim(t["id"], later) == [t["id"]]
        return new_workflow_run(store, *args, **kwargs)

    monkeypatch.setattr(scheduler.workflows_api, "new_workflow_run", reclaim_then_create)
    fire(t["id"], now)
    monkeypatch.setattr(scheduler.workflows_api, "new_workflow_run", new_workflow_run)

    # Neither committed nor queued; the trigger shows the newer firing.
    assert workflow_runs(in_store, workflow["id"]) == [] and queued == []
    t1 = load(db, t["id"])
    assert (t1.last_fired_at, t1.last_status, t1.last_workflow_run_id) == (
        later,
        TriggerStatus.running,
        None,
    )

    fire(t["id"], later)
    (run,) = workflow_runs(in_store, workflow["id"])
    assert queued == [run.id]
    t2 = load(db, t["id"])
    assert (t2.last_fired_at, t2.last_workflow_run_id) == (later, run.id)


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_a_reclaim_while_a_run_is_being_started_waits_for_the_next_tick(
    api, db, queued, monkeypatch, kind, in_store
):
    """The other interleaving: the re-claim comes while the start holds the trigger's lock."""
    target = make_agent(api, "Bot") if kind == "agent" else make_runnable_workflow(api)
    t = make_trigger(api, schedule(kind, target["id"]))
    now = datetime.now(UTC)
    later = now + timedelta(minutes=1)
    claim(db, t["id"], now)
    claimed = scheduler._claimed
    attempts = []

    def claimed_then_reclaim(db, trigger_id, fired_at, kind=None, *, lock=False):
        if lock:
            # Due again by the time this firing locks the trigger to link its run.
            with SessionLocal() as s:
                make_due(s, trigger_id, later - timedelta(seconds=1))
        trigger = claimed(db, trigger_id, fired_at, kind, lock=lock)
        if lock:
            with SessionLocal() as s:
                attempts.append(claimed_ids(s, later))
        return trigger

    monkeypatch.setattr(scheduler, "_claimed", claimed_then_reclaim)
    with use_model(replies("hi")):
        fire(t["id"], now)
    monkeypatch.setattr(scheduler, "_claimed", claimed)

    # Skipped while locked, so this firing keeps its run and the link to it...
    assert attempts == [[]]
    fired = load(db, t["id"])
    if kind == "agent":
        (run,) = agent_runs(in_store, target["id"])
        assert (fired.last_fired_at, fired.last_status, fired.last_run_id) == (
            now,
            TriggerStatus.succeeded,
            run.id,
        )
    else:
        (run,) = workflow_runs(in_store, target["id"])
        assert (fired.last_fired_at, fired.last_workflow_run_id) == (now, run.id)
        assert queued == [run.id]
    # ...and the schedule is still due: claimed on the next tick, not lost.
    with SessionLocal() as s:
        assert claimed_ids(s, later) == [t["id"]]
    reclaimed = load(db, t["id"])
    assert (reclaimed.last_fired_at, reclaimed.last_run_id) == (later, None)
