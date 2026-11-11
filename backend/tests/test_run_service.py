"""The run service (`aios.run_service`), through its drain-once entry point and its queue: fair
claiming across organisations, the in-flight cap and its admin override, the interactive
reserve, leases, retries and the attempt limit, and schedules firing once across processes."""

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import org_of, signed_in_store
from sqlalchemy import func, select, update
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_chat, messages, signed_in_as
from test_organisation_scoping import member  # noqa: F401
from test_permissions import set_role
from test_scheduler import load, make_due
from test_triggers import make_agent, make_trigger, schedule

from aios import checkpoints, scheduler, turn_lease
from aios.config import get_settings
from aios.datastores import data_store_for
from aios.datastores.models import AgentRun, Invocation
from aios.db import SessionLocal
from aios.models import Role, TriggerStatus, WorkItem, WorkKind, WorkLane
from aios.run_service import STOPPED, executor, queue, work
from aios.testing.fake_model import model, replies, use_model


def org_and_store(db, user) -> tuple[int, uuid.UUID]:
    org = org_of(db, user)
    return org.id, data_store_for(org.id)


def items(org: tuple[int, uuid.UUID], lane: WorkLane, n: int, tag: str) -> list[queue.Work]:
    organisation_id, store_id = org
    kind = WorkKind.invocation_start if lane == WorkLane.interactive else WorkKind.agent_turn
    return [
        queue.Work(organisation_id, store_id, lane, kind, {"tag": tag, "n": i}) for i in range(n)
    ]


def enqueue(works: list[queue.Work]) -> None:
    with SessionLocal() as db:
        queue.enqueue(db, works)
        db.commit()


def claim(limit: int, background_limit: int | None = None) -> list[queue.Claimed]:
    with SessionLocal() as db:
        return queue.claim(db, limit, background_limit)


def tags(claimed: list[queue.Claimed]) -> list[str]:
    return [c.payload["tag"] for c in claimed]


class Recorder:
    """Stands in for every kind of work: notes when each item started, and takes `seconds`."""

    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds = seconds
        self.started: dict[str, list[float]] = {}
        self.release = threading.Event()
        self.blocking = False

    async def __call__(self, item: queue.Claimed) -> work.Outcome:
        self.started.setdefault(item.payload["tag"], []).append(time.monotonic())
        if self.blocking:
            await asyncio.to_thread(self.release.wait, 10)
        await asyncio.sleep(self.seconds)
        return work.Done()


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    stub = Recorder()
    monkeypatch.setitem(work.HANDLERS, WorkKind.invocation_start, stub)
    monkeypatch.setitem(work.HANDLERS, WorkKind.agent_turn, stub)
    return stub


def test_a_mentioned_turn_starts_promptly_while_another_organisation_floods_the_queue(
    db, user, new_user, recorder
):
    flooding, chatting = org_and_store(db, user), org_and_store(db, new_user())
    recorder.seconds = 0.02
    enqueue(items(flooding, WorkLane.background, 1000, "flood"))

    draining = threading.Thread(target=lambda: asyncio.run(executor.drain_once()))
    draining.start()
    try:
        while "flood" not in recorder.started:
            time.sleep(0.01)
        # Mid-flood, someone in another organisation mentions an agent.
        asked_at = time.monotonic()
        enqueue(items(chatting, WorkLane.interactive, 1, "mention"))
        while "mention" not in recorder.started:
            assert time.monotonic() - asked_at < 30
            time.sleep(0.01)
    finally:
        draining.join()
    waited = recorder.started["mention"][0] - asked_at
    assert waited < get_settings().queue_chat_start_target_seconds
    # The flood was still going when it started, and all of it ran.
    assert len(recorder.started["flood"]) == 1000
    assert max(recorder.started["flood"]) > recorder.started["mention"][0]


def test_chat_comes_first_then_the_organisation_with_least_in_flight(db, user, new_user):
    busy, quiet = org_and_store(db, user), org_and_store(db, new_user())
    enqueue(items(busy, WorkLane.background, 3, "busy-background"))
    enqueue(items(quiet, WorkLane.background, 1, "quiet-background"))
    enqueue(items(busy, WorkLane.interactive, 1, "busy-chat"))
    [first] = claim(1)
    assert tags([first]) == ["busy-chat"]
    # Now `busy` has one in flight and `quiet` none: `quiet` goes first, though it's newer.
    assert tags(claim(2)) == ["quiet-background", "busy-background"]


def test_an_organisation_at_its_cap_waits_while_others_are_claimed(db, user, new_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "queue_in_flight_cap", 2)
    capped, other = org_and_store(db, user), org_and_store(db, new_user())
    enqueue(items(capped, WorkLane.background, 5, "capped"))
    enqueue(items(other, WorkLane.background, 5, "other"))
    assert sorted(tags(claim(10))) == ["capped"] * 2 + ["other"] * 2
    # Neither has room until one of theirs is done.
    assert claim(10) == []


def test_each_process_keeps_room_for_people_waiting_in_chat(db, user, recorder):
    org = org_and_store(db, user)
    recorder.blocking = True
    enqueue(items(org, WorkLane.background, 10, "background"))

    async def go():
        process = executor.Executor(concurrency=5, reserve=2)
        await process.claim()
        # Background work takes at most 3 of its 5 slots, however much is waiting.
        assert len(process.running) == 3
        await process.claim()
        assert len(process.running) == 3
        await asyncio.to_thread(enqueue, items(org, WorkLane.interactive, 3, "chat"))
        await process.claim()
        running = [item.payload["tag"] for item, _ in process.running.values()]
        assert sorted(running) == ["background"] * 3 + ["chat"] * 2
        recorder.release.set()
        await asyncio.gather(*(task for _, task in process.running.values()))

    asyncio.run(go())


def test_an_admin_raises_their_organisations_cap_and_more_is_then_claimed_at_once(
    api,
    db,
    user,
    member,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "queue_in_flight_cap", 2)
    org = org_and_store(db, user)
    assert api.get("/api/organisation").json()["in_flight_cap"] == 2
    enqueue(items(org, WorkLane.background, 6, "work"))
    assert len(claim(10)) == 2

    for role in (Role.editor, Role.participant):
        set_role(db, member, role)
        with signed_in_as(member):
            resp = api.put("/api/organisation/in-flight-cap", json={"in_flight_cap": 50})
        assert resp.status_code == 403, resp.text
    assert len(claim(10)) == 0

    resp = api.put("/api/organisation/in-flight-cap", json={"in_flight_cap": 4})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["in_flight_cap"], resp.json()["in_flight_cap_overridden"]) == (4, True)
    assert len(claim(10)) == 2

    # Back to the default.
    resp = api.put("/api/organisation/in-flight-cap", json={"in_flight_cap": None})
    assert (resp.json()["in_flight_cap"], resp.json()["in_flight_cap_overridden"]) == (2, False)


def test_enqueueing_is_idempotent_by_its_deduplication_key(db, user):
    org_id, store_id = org_and_store(db, user)
    twice = queue.Work(org_id, store_id, WorkLane.interactive, WorkKind.agent_turn, {}, "k")
    enqueue([twice])
    enqueue([twice])
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).where(WorkItem.dedup_key == "k")) == 1


def die(claimed: list[queue.Claimed]) -> None:
    """The process that claimed these items dies: their leases lapse."""
    with SessionLocal() as db:
        db.execute(
            update(WorkItem)
            .where(WorkItem.id.in_([c.id for c in claimed]))
            .values(leased_until=func.now() - timedelta(seconds=1))
        )
        db.commit()


def test_an_item_whose_lease_lapses_is_retried_elsewhere(db, user, recorder):
    enqueue(items(org_and_store(db, user), WorkLane.interactive, 1, "job"))
    [claimed] = claim(1)
    # Leased: no other process takes it.
    assert claim(1) == []
    die([claimed])
    asyncio.run(executor.drain_once())
    assert len(recorder.started["job"]) == 1
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(WorkItem)) == 0


def test_after_three_attempts_a_mentions_invocation_fails_with_the_service_stopped(
    api, run_service
):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    run_service.auto = False
    say(api, chat, "Go", ("agent", agent))
    # Three processes claim its start in turn, and each dies.
    for _ in range(get_settings().queue_max_attempts):
        die(claim(1))
    run_service.drain()
    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("failed", STOPPED)
    assert replies_from(api, chat) == [("Bot", f"Couldn't start the run: {STOPPED}")]
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(WorkItem)) == 0


def test_a_turn_whose_process_dies_mid_turn_carries_on_elsewhere_from_its_last_checkpoint(
    api, in_store, run_service
):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    run_service.auto = False
    say(api, chat, "Go", ("agent", agent))
    calls, hung, dead = [], threading.Event(), threading.Event()

    async def script(msgs, info):
        calls.append([m.type for m in msgs if m.type != "system"])
        if len(calls) == 1:
            # The first process hangs mid-call, and is then taken for dead.
            hung.set()
            await asyncio.to_thread(dead.wait, 30)
            raise RuntimeError("the first process is gone")
        yield "Recovered."

    with use_model(model(script)):
        first = threading.Thread(target=run_service.drain)
        first.start()
        try:
            assert hung.wait(30)
            # Its leases lapse: the item's, and the run's turn lease.
            with SessionLocal() as s:
                s.execute(update(WorkItem).values(leased_until=func.now() - timedelta(seconds=1)))
                s.commit()
            in_store.execute(update(AgentRun).values(lease_until=func.now()))
            # Another process claims it again, and carries the turn on.
            run_service.drain()
        finally:
            dead.set()
            first.join()

    [invocation] = messages(api, chat)["invocations"]
    assert (invocation["status"], invocation["error"]) == ("succeeded", None)
    assert replies_from(api, chat) == [("Bot", "Recovered.")]
    # The user's message was given to the run once, and the turn carried on from it.
    history = checkpoints.history(in_store.store_id, invocation["agent_run_id"])
    assert [m.type for m in history] == ["human", "ai"]
    assert calls[1] == ["human"]
    assert invocation["agent_run"]["status"] == "succeeded"


def test_a_run_busy_with_another_turn_is_handed_back_without_counting_an_attempt(
    api, run_service, monkeypatch
):
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    run_service.auto = False
    say(api, chat, "Go", ("agent", agent))
    acquire = turn_lease.acquire

    async def busy_once(store_id, run_id, wait=None):
        monkeypatch.setattr(turn_lease, "acquire", acquire)
        raise turn_lease.RunBusy

    monkeypatch.setattr(turn_lease, "acquire", busy_once)
    with use_model(replies("Hi.")):
        run_service.drain()
    with SessionLocal() as s:
        [waiting] = s.scalars(select(WorkItem)).all()
        assert (waiting.kind, waiting.attempts, waiting.lease_token) == (
            WorkKind.agent_turn,
            0,
            None,
        )
        assert waiting.ready_at > s.scalar(select(func.now()))
        s.execute(update(WorkItem).values(ready_at=func.now()))
        s.commit()
    with use_model(replies("Hi.")):
        run_service.drain()
    assert replies_from(api, chat) == [("Bot", "Hi.")]


def test_each_run_of_a_schedule_fires_once_across_run_service_processes(api, db, in_store):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    claimed, barrier = [], threading.Barrier(4)

    def process():
        barrier.wait()
        claimed.extend(asyncio.run(scheduler.tick(now)))

    processes = [threading.Thread(target=process) for _ in range(4)]
    for p in processes:
        p.start()
    for p in processes:
        p.join()
    assert claimed == [(signed_in_store(), t["id"])]
    with use_model(replies("Done.")):
        asyncio.run(executor.drain_once())
    runs = in_store.scalars(select(AgentRun).where(AgentRun.agent_id == agent["id"])).all()
    assert len(runs) == 1
    assert load(db, t["id"]).last_status == TriggerStatus.succeeded


def test_a_schedules_turn_whose_attempts_run_out_fails_its_firing(api, db, in_store):
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    make_due(db, t["id"], now - timedelta(seconds=1))
    asyncio.run(scheduler.tick(now))
    for _ in range(get_settings().queue_max_attempts):
        die(claim(1))
    asyncio.run(executor.drain_once())
    fired = load(db, t["id"])
    assert (fired.last_status, fired.last_error) == (TriggerStatus.failed, STOPPED)
    [invocation] = in_store.scalars(
        select(Invocation).where(Invocation.trigger_id == t["id"])
    ).all()
    assert (invocation.status, invocation.error) == ("failed", STOPPED)
