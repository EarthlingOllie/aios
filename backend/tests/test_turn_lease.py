"""The turn lease (`aios.turn_lease`): a run's turns take turns through a lease on the run's row,
refreshed by the turn's heartbeat, waited on through Redis, and fenced, so a turn that lost it
can't write the run's history. Stopping a turn signals its holder; no connection is ended.

Two turns posted to one run at once are in test_agents (`test_concurrent_turns_on_a_run_take_
turns`), and a turn going on while its agent is archived in test_agent_archive."""

import asyncio
import json
import time

import pytest
from conftest import org_of
from sqlalchemy import func, select, text, update
from test_agents import chat_body, make_agent

from aios import checkpoints, turn_lease
from aios.api import agents as agents_api
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import AgentRun
from aios.testing.fake_model import model, use_model


def new_run(api) -> tuple[dict, dict]:
    agent = make_agent(api, "Bot")
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    return agent, run


def chunks_of(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: {")
    ]


async def consume(response) -> list[dict]:
    return chunks_of("".join([chunk async for chunk in response.body_iterator]))


def history(run: dict) -> list[str]:
    return [m.content for m in checkpoints.history(run["id"])]


def lease_of(run: dict) -> tuple:
    with SessionLocal() as db:
        return db.execute(
            select(AgentRun.lease_token, AgentRun.lease_until).where(AgentRun.id == run["id"])
        ).one()


ADVISORY_LOCKS = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


def advisory_locks() -> int:
    """Advisory locks held on the platform database, by any session."""
    with SessionLocal() as db:
        return db.scalar(ADVISORY_LOCKS)


def test_the_lease_is_free_after_a_turn_and_no_advisory_lock_is_taken(api, user):
    agent, run = new_run(api)
    seen = {}

    async def script(messages, info):
        seen["lease"] = await asyncio.to_thread(lease_of, run)
        seen["advisory"] = await asyncio.to_thread(advisory_locks)
        yield "hi"

    with use_model(model(script)):
        resp = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi"))
    assert resp.status_code == 200, resp.text

    token, until = seen["lease"]
    assert token is not None and until is not None
    assert seen["advisory"] == 0
    assert lease_of(run) == (None, None)
    assert history(run) == ["hi", "hi"]


def test_a_run_busy_past_the_wait_answers_409(api, user, monkeypatch):
    agent, run = new_run(api)
    monkeypatch.setattr(get_settings(), "turn_wait_seconds", 0.5)
    # Another turn (another replica's, say) holds the run.
    assert turn_lease.try_acquire(run["id"]) is not None

    started = time.monotonic()
    resp = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "This run is busy"
    assert time.monotonic() - started >= 0.5
    assert history(run) == []


def test_a_waiting_turn_starts_as_soon_as_the_holder_releases(api, user):
    _, run = new_run(api)

    async def scenario() -> float:
        holder = await turn_lease.acquire(run["id"])
        waiter = asyncio.create_task(turn_lease.acquire(run["id"], wait=30))
        await asyncio.sleep(0.3)
        assert not waiter.done()
        released = time.monotonic()
        await turn_lease.release(holder)
        lease = await asyncio.wait_for(waiter, timeout=5)
        took = time.monotonic() - released
        await turn_lease.release(lease)
        return took

    # Told by the release channel, not the one-second poll.
    assert asyncio.run(scenario()) < 0.5


def test_an_expired_lease_is_free_and_isnt_a_turn_underway(api, user):
    _, run = new_run(api)
    stale = turn_lease.try_acquire(run["id"])
    with SessionLocal() as db:
        assert checkpoints.turns_underway(db, [run["id"]]) == {run["id"]}
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run["id"])
            .values(lease_until=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )
        db.commit()
        assert checkpoints.turns_underway(db, [run["id"]]) == set()

    taken = turn_lease.try_acquire(run["id"])
    assert taken is not None and taken.token != stale.token
    assert turn_lease.refresh(stale) is False


def expire(run: dict) -> None:
    """The holder froze past its lease's expiry."""
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run["id"])
            .values(lease_until=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )
        db.commit()


def test_a_holder_whose_lease_lapsed_cant_write_a_checkpoint(api, user):
    """Frozen past its lease, which another turn then took: its writes fail, storing nothing."""
    agent, run = new_run(api)
    taken = {}

    async def script(messages, info):
        await asyncio.to_thread(expire, run)
        taken["lease"] = await asyncio.to_thread(turn_lease.try_acquire, run["id"])
        yield "too late"

    with use_model(model(script)):
        resp = api.post(f"/api/agents/{agent['id']}/runs/{run['id']}/chat", json=chat_body("hi"))

    assert taken["lease"] is not None
    assert any(c["type"] == "error" for c in chunks_of(resp.text))
    # The user's message was checkpointed before the model was called, while it held the lease;
    # the reply, and the record of the unfinished turn, weren't.
    assert history(run) == ["hi"]
    # The new holder's lease is untouched by the stale one's ending.
    assert lease_of(run)[0] == taken["lease"].token


def test_a_stale_holders_checkpoint_write_fails_and_stores_nothing(api, user):
    _, run = new_run(api)

    async def scenario() -> None:
        async with checkpoints.turn(run["id"]) as saver:
            config = {
                **checkpoints.thread_config(run["id"]),
                "configurable": {
                    "thread_id": checkpoints.thread_id(run["id"]),
                    "checkpoint_ns": "",
                    "checkpoint_id": "1",
                },
            }
            await asyncio.to_thread(expire, run)
            with pytest.raises(turn_lease.LeaseLost):
                await saver.aput_writes(config, [("messages", ["x"])], "task")
            count = await saver.conn.execute(
                "SELECT count(*) AS n FROM checkpoint_writes WHERE thread_id = %s",
                (checkpoints.thread_id(run["id"]),),
            )
            assert (await count.fetchone())["n"] == 0

    asyncio.run(scenario())


def slow_turn(run: dict, started: asyncio.Event, go_on: asyncio.Event | None = None):
    async def script(messages, info):
        started.set()
        if go_on is None:
            await asyncio.sleep(30)
        else:
            await go_on.wait()
        yield "after the stop"

    return model(script)


def test_stopping_a_turn_ends_it_within_a_second(api, user):
    agent, run = new_run(api)
    started = asyncio.Event()
    seen = {}

    async def scenario() -> list[dict]:
        with SessionLocal() as db:
            response = await agents_api.run_chat(
                agent["id"], run["id"], chat_body("hi"), user, db, org_of(db, user)
            )
        turn = asyncio.create_task(consume(response))
        await started.wait()
        with SessionLocal() as db:
            stopped_at = time.monotonic()
            await asyncio.to_thread(checkpoints.stop_turns, db, [run["id"]])
        chunks = await asyncio.wait_for(turn, timeout=5)
        seen["took"] = time.monotonic() - stopped_at
        return chunks

    with use_model(slow_turn(run, started)):
        chunks = asyncio.run(scenario())

    assert seen["took"] < 1
    errors = [c["errorText"] for c in chunks if c["type"] == "error"]
    assert errors == ["This turn was stopped."]
    assert "after the stop" not in json.dumps(chunks)
    assert lease_of(run) == (None, None)
    assert history(run)[0] == "hi"
    out = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    assert out["status"] == "failed"


def test_stopping_a_turn_signals_its_holder_and_leaves_its_connection_working(api, user):
    _, run = new_run(api)
    seen = {}

    async def scenario() -> float:
        entered = asyncio.Event()

        async def holder() -> None:
            async with checkpoints.turn(run["id"]) as saver:
                entered.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    # Cancelled by the stop signal, on a connection nothing ended.
                    cursor = await saver.conn.execute("SELECT 1 AS one")
                    seen["alive"] = (await cursor.fetchone())["one"] == 1
                    raise

        task = asyncio.create_task(holder())
        await entered.wait()
        with SessionLocal() as db:
            stopped_at = time.monotonic()
            await asyncio.to_thread(checkpoints.stop_turns, db, [run["id"]])
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        return time.monotonic() - stopped_at

    assert asyncio.run(scenario()) < 1
    assert seen["alive"] is True


def test_with_redis_down_a_stopped_holders_next_write_fails_on_fencing(api, user, monkeypatch):
    agent, run = new_run(api)
    started, go_on = asyncio.Event(), asyncio.Event()
    # Nothing listens there: the stop signal can't be sent, nor heard.
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")

    async def scenario() -> list[dict]:
        with SessionLocal() as db:
            response = await agents_api.run_chat(
                agent["id"], run["id"], chat_body("hi"), user, db, org_of(db, user)
            )
        turn = asyncio.create_task(consume(response))
        await started.wait()
        with SessionLocal() as db:
            await asyncio.to_thread(checkpoints.stop_turns, db, [run["id"]])
        await asyncio.sleep(0.2)
        assert not turn.done(), "nothing told the holder"
        go_on.set()
        return await asyncio.wait_for(turn, timeout=10)

    with use_model(slow_turn(run, started, go_on)):
        chunks = asyncio.run(scenario())

    assert any(c["type"] == "error" for c in chunks)
    # Its reply was never stored.
    assert history(run) == ["hi"]


def test_the_checkpointer_setup_holds_its_lock_for_one_transaction(db, monkeypatch):
    monkeypatch.setattr(checkpoints, "_ready", False)
    asyncio.run(checkpoints.ensure_ready())
    assert checkpoints._ready
    assert advisory_locks() == 0
