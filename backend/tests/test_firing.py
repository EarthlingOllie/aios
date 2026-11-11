"""The firing machinery (`aios.firing`) driven by a cause that isn't a schedule: a stub standing
in for, say, a chat mention. What it starts, beats, fails and records is the same as for a
schedule (`test_scheduler.py`); only the cause's own parts are the stub's."""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from conftest import home_organisation, store_of, user_store
from sqlalchemy import func, select, update
from test_agents import chat_body
from test_triggers import make_agent

from aios import checkpoints, firing
from aios.api.agents import new_agent_run
from aios.config import get_settings
from aios.datastores import organisation_store, store_id_of
from aios.datastores.models import Agent, AgentRun
from aios.models import AgentRunStatus
from aios.testing.fake_model import model, replies, use_model


@dataclass
class Mention:
    """A stub cause: starts a run of the agent with the message, and keeps what it's told."""

    user_id: int
    agent_id: int
    message: str
    cant_start: str | None = None
    # Why its preparation fails, if it does (as a chat mention's handoff can).
    cant_prepare: str | None = None
    prepared: bool = False
    run_id: int | None = None
    beats: int = 0
    outcomes: list[str | None] = field(default_factory=list)

    async def prepare(self) -> None:
        if self.cant_prepare:
            raise firing.CantStart(self.cant_prepare)
        self.prepared = True

    def start(self, db) -> firing.Turn:
        assert self.prepared
        if self.cant_start:
            raise firing.CantStart(self.cant_start)
        org_id = home_organisation(db, self.user_id).id
        with organisation_store(org_id) as store:
            agent = store.get(Agent, self.agent_id)
            run = new_agent_run(store, org_id, agent, created_by=self.user_id)
            store.commit()
            store_id = store_id_of(store)
        self.run_id = run.id
        return firing.Turn(store_id, self.agent_id, run.id, self.message)

    def beat(self, db) -> None:
        self.beats += 1

    def record(self, db, error: str | None) -> None:
        self.outcomes.append(error)


def run_row(user, run_id: int) -> AgentRun:
    with user_store(user) as store:
        return store.get(AgentRun, run_id)


def runs_of(user, agent_id: int) -> list[AgentRun]:
    with user_store(user) as store:
        return store.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id)).all()


def test_a_cause_starts_a_run_runs_its_turn_and_records_the_outcome(api, user, db):
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "What's new?")

    with use_model(replies("Nothing.")):
        asyncio.run(firing.fire(mention))

    assert mention.outcomes == [None]
    history = checkpoints.history(store_of(user), mention.run_id)
    assert [(m.type, m.text) for m in history] == [("human", "What's new?"), ("ai", "Nothing.")]
    shown = api.get(f"/api/agents/{agent['id']}/runs/{mention.run_id}").json()
    assert (shown["status"], shown["title"], shown["message_count"]) == (
        "succeeded",
        "What's new?",
        2,
    )
    assert shown["finished_at"] is not None


def test_a_failed_turn_is_recorded_with_its_error(api, user, db):
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "Go")

    async def script(messages, info):
        yield "Starting"
        raise RuntimeError("model overloaded")

    with use_model(model(script)):
        asyncio.run(firing.fire(mention))

    assert mention.outcomes == ["model overloaded"]
    assert run_row(user, mention.run_id).status == AgentRunStatus.failed


def test_a_cause_that_cant_start_records_why_without_a_run(api, user, db):
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "Go", cant_start="The chat has been deleted.")
    asyncio.run(firing.fire(mention))
    assert mention.outcomes == ["The chat has been deleted."]
    assert runs_of(user, agent["id"]) == []


def test_a_cause_that_cant_prepare_records_why_and_starts_nothing(api, user, db):
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "Go", cant_prepare="The handoff failed: overloaded")
    asyncio.run(firing.fire(mention))
    assert mention.outcomes == ["The handoff failed: overloaded"]
    assert runs_of(user, agent["id"]) == []


def age(user, run_id: int) -> None:
    """Make the run's heartbeat older than a live process would let it get."""
    with user_store(user) as store:
        store.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(heartbeat_at=func.now() - firing.STALE_AFTER - timedelta(seconds=1))
        )
        store.commit()


def test_a_launched_fire_takes_a_slot_and_is_kept_alive_while_it_runs(api, user, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "scheduler_max_concurrent_runs", 2)
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "Go")
    release = None

    async def script(messages, info):
        await release.wait()
        yield "done"

    def other_replica_sweeps():
        firing.fail_stalled(store_of(user), [mention.run_id])

    async def go():
        nonlocal release
        release = asyncio.Event()
        assert firing.free_slots() == 2
        task = firing.launch(mention, name="mention")
        assert firing.free_slots() == 1
        while mention.run_id is None or run_row(user, mention.run_id).heartbeat_at is None:
            await asyncio.sleep(0.01)
        # Long enough without a beat that it would count as stopped; this process beats it.
        await asyncio.to_thread(age, user, mention.run_id)
        await firing.beat()
        await asyncio.to_thread(other_replica_sweeps)
        assert (await asyncio.to_thread(run_row, user, mention.run_id)).status == (
            AgentRunStatus.running
        )
        release.set()
        await task
        assert firing.free_slots() == 2

    with use_model(model(script)):
        asyncio.run(go())
    assert mention.beats >= 1
    assert mention.outcomes == [None]
    assert run_row(user, mention.run_id).status == AgentRunStatus.succeeded


def test_a_run_whose_process_died_is_failed_once_its_heartbeat_stops(api, user, db):
    agent = make_agent(api, "Bot")
    runs_url = f"/api/agents/{agent['id']}/runs"
    # Three runs mid-turn: one whose process died, one still beating, and one bound to a request
    # (no heartbeat: it ends with the request).
    dead, alive, chatting = (api.post(runs_url).json()["id"] for _ in range(3))
    with user_store(user) as store:
        store.execute(
            update(AgentRun)
            .where(AgentRun.id.in_([dead, alive, chatting]))
            .values(status=AgentRunStatus.running, started_at=func.now())
        )
        store.execute(update(AgentRun).where(AgentRun.id == alive).values(heartbeat_at=func.now()))
        store.commit()
    age(user, dead)

    firing.fail_stalled(store_of(user), [dead, alive, chatting])

    stopped = run_row(user, dead)
    assert stopped.status == AgentRunStatus.failed
    assert stopped.finished_at is not None
    assert run_row(user, alive).status == AgentRunStatus.running
    assert run_row(user, chatting).status == AgentRunStatus.running
    assert api.get(f"{runs_url}/{dead}").json()["status"] == "failed"


def test_a_chat_turn_on_a_fired_run_isnt_failed_as_stalled(api, user, db):
    """A run started by a cause can be continued from the UI; that turn is the request's, and
    ends with it, so the heartbeat the cause's turn kept doesn't fail it."""
    agent = make_agent(api, "Bot")
    mention = Mention(user.id, agent["id"], "Go")
    with use_model(replies("done")):
        asyncio.run(firing.fire(mention))
    assert run_row(user, mention.run_id).heartbeat_at is not None
    # Hours later, the user picks the run up.
    age(user, mention.run_id)
    seen = []

    async def script(messages, info):
        firing.fail_stalled(store_of(user), [mention.run_id])
        seen.append(run_row(user, mention.run_id).status)
        yield "still here"

    with use_model(model(script)):
        resp = api.post(
            f"/api/agents/{agent['id']}/runs/{mention.run_id}/chat", json=chat_body("More?")
        )
    assert resp.status_code == 200
    assert seen == [AgentRunStatus.running]
    assert run_row(user, mention.run_id).status == AgentRunStatus.succeeded
