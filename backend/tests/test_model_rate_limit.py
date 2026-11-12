"""The shared model rate limit (`aios.model_rate_limit`): a token bucket per model in Redis keeps
every run service process's calls to a model within its requests and tokens per minute; calls
waiting are served people in chat first, then in the order they came; and a model with no limit
set isn't paced at all."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest
import redis
from test_chat_agent_invocations import replies_from, say
from test_chats import add, make_chat
from test_triggers import make_agent

from aios import model_rate_limit
from aios.config import ModelRateLimit, get_settings
from aios.models import WorkLane
from aios.testing.fake_model import replies, use_model

MODEL = "anthropic:claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(get_settings(), "model_rate_limits", {})
    model_rate_limit.reset()
    yield
    model_rate_limit.reset()


def limit(monkeypatch, model: str = MODEL, **values) -> None:
    monkeypatch.setattr(get_settings(), "model_rate_limits", {model: ModelRateLimit(**values)})


def keys() -> list[bytes]:
    with redis.Redis.from_url(get_settings().redis_url) as client:
        return client.keys(f"{model_rate_limit.PREFIX}*")


def test_a_model_with_no_limit_set_isnt_paced(monkeypatch):
    limit(monkeypatch, "anthropic:another-model", requests_per_minute=1)

    async def many() -> float:
        started = time.monotonic()
        for _ in range(200):
            await model_rate_limit.acquire(MODEL)
            await model_rate_limit.spend(MODEL, 10_000)
        return time.monotonic() - started

    assert asyncio.run(many()) < 0.5
    assert keys() == []


PROCESS = textwrap.dedent(
    """
    import asyncio, time
    from aios import model_rate_limit

    async def main():
        got = 0
        until = time.monotonic() + {seconds}
        while True:
            try:
                await asyncio.wait_for(
                    model_rate_limit.acquire({model!r}), until - time.monotonic()
                )
            except (TimeoutError, ValueError):
                break
            got += 1
        print(got)

    asyncio.run(main())
    """
)


def test_calls_across_two_processes_stay_within_the_models_limit():
    """Six requests a minute: in two seconds, two processes asking as fast as they can get six
    between them (a minute's worth to start with, and none refilled yet), not six each."""
    env = {
        **os.environ,
        "REDIS_URL": get_settings().redis_url,
        "MODEL_RATE_LIMITS": json.dumps({MODEL: {"requests_per_minute": 6}}),
    }
    code = PROCESS.format(seconds=2.0, model=MODEL)
    processes = [
        subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    counts = [int(p.communicate(timeout=60)[0].strip()) for p in processes]
    assert sum(counts) == 6, counts


def test_tokens_spent_hold_the_next_call_until_the_bucket_refills(monkeypatch):
    limit(monkeypatch, tokens_per_minute=600)  # ten a second

    async def scenario() -> float:
        await model_rate_limit.acquire(MODEL)
        await model_rate_limit.spend(MODEL, 600 + 3)  # all of it, and three over
        started = time.monotonic()
        await model_rate_limit.acquire(MODEL)
        return time.monotonic() - started

    # Back above nothing after the three over and one more: about 0.4 seconds.
    assert 0.3 < asyncio.run(scenario()) < 2


def test_people_waiting_in_chat_are_served_before_background_work(monkeypatch):
    limit(monkeypatch, requests_per_minute=120)  # one every half second
    served: list[str] = []

    async def call(who: str, lane: WorkLane, after: float) -> None:
        await asyncio.sleep(after)
        with model_rate_limit.serving(lane):
            await model_rate_limit.acquire(MODEL)
        served.append(who)

    async def scenario() -> None:
        for _ in range(120):  # a minute's worth, used up
            await model_rate_limit.acquire(MODEL)
        await asyncio.gather(
            call("schedule", WorkLane.background, 0),
            call("another schedule", WorkLane.background, 0.05),
            call("mention", WorkLane.interactive, 0.1),
        )

    asyncio.run(scenario())
    assert served == ["mention", "schedule", "another schedule"]


def test_a_mentioned_agents_turn_is_paced_as_chat_work(api, monkeypatch):
    limit(monkeypatch, requests_per_minute=60)
    seen: list[tuple[str, int]] = []
    acquire = model_rate_limit.acquire

    async def spy(model: str) -> None:
        seen.append((model, model_rate_limit.priority()))
        await acquire(model)

    monkeypatch.setattr(model_rate_limit, "acquire", spy)
    agent = make_agent(api, "Bot")
    chat = make_chat(api, "Design")
    add(api, chat, "agent", agent)
    with use_model(replies("Hi.")):
        say(api, chat, "Hello", ("agent", agent))

    assert replies_from(api, chat) == [("Bot", "Hi.")]
    # The handoff's call and the turn's, each as chat work.
    interactive = model_rate_limit.priority_of(WorkLane.interactive)
    assert seen == [(MODEL, interactive), (MODEL, interactive)]
    # Both taken from the model's bucket (less the little it refilled meanwhile).
    assert model_rate_limit.requests_left(MODEL) < 60 - 1.5
