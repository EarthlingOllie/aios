"""The shared model rate limit: a token bucket per model in Redis, so every run service process's
calls to a model stay within the requests and tokens per minute its provider allows
(`model_rate_limits`). A model with no limit set isn't paced at all, and nothing touches Redis
for it.

- **The bucket** holds a minute's worth of each limit and refills continuously. A call takes a
  request before it's made (`acquire`), and needs some tokens left; once it's answered, the
  tokens it used are taken (`spend`), which may leave the bucket owing, so the next call waits
  until it has refilled past what was owed.
- **Waiting calls are served in the same order as claims**: people waiting in chat first (the
  interactive lane), then the oldest. Each waiter is in a sorted set per model, and only the
  first may take from the bucket. A waiter that stops asking (its process died) drops out after
  `STALE_MS`.
- The lane a call is made for is the run service item's it's made under (`serving`, set by
  `aios.run_service.executor`); outside an item it counts as background work.
- If Redis can't be reached the call goes ahead unpaced: a limiter that failed closed would stop
  every turn.

Every step is one Lua script, so processes taking from a bucket at once never both take its
last request. Times are Redis's own, so processes' clocks don't matter.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar

import redis
import redis.asyncio as aioredis
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from aios.config import ModelRateLimit, get_settings
from aios.models import WorkLane

log = logging.getLogger(__name__)

PREFIX = "model-rate:"
# A waiter that hasn't asked again for this long (milliseconds) has gone, and drops out.
STALE_MS = 5000
# How long a waiter that isn't first waits before asking again (seconds); one that is first
# waits for as long as the bucket says, up to `MAX_WAIT`.
POLL = 0.02
MAX_WAIT = 0.25

# Refills the bucket (`KEYS[3]`, a hash of requests, tokens and when) to now; `ARGV[4]` and
# `ARGV[5]` are the limits per minute (0: none). Shared by both scripts.
_REFILL = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local rpm = tonumber(ARGV[4])
local tpm = tonumber(ARGV[5])
local b = redis.call('HMGET', KEYS[3], 'req', 'tok', 'at')
local at = tonumber(b[3]) or now
local req = tonumber(b[1]) or rpm
local tok = tonumber(b[2]) or tpm
local dt = math.max(0, now - at)
if rpm > 0 then req = math.min(rpm, req + dt * rpm / 60000) end
if tpm > 0 then tok = math.min(tpm, tok + dt * tpm / 60000) end
"""

_SAVE = """
redis.call('HSET', KEYS[3], 'req', tostring(req), 'tok', tostring(tok), 'at', now)
redis.call('PEXPIRE', KEYS[3], 120000)
"""

# KEYS: waiters (by priority, then arrival), when each last asked, the bucket. ARGV: the
# waiter, its score, the stale age, the limits. Returns 0 once it has taken a request, else how
# long to wait (milliseconds; -1 while another waiter is first).
_ACQUIRE = (
    _REFILL
    + """
redis.call('ZADD', KEYS[1], 'NX', ARGV[2], ARGV[1])
redis.call('ZADD', KEYS[2], now, ARGV[1])
local stale = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now - tonumber(ARGV[3]))
for _, w in ipairs(stale) do
  redis.call('ZREM', KEYS[1], w)
  redis.call('ZREM', KEYS[2], w)
end
redis.call('PEXPIRE', KEYS[1], 120000)
redis.call('PEXPIRE', KEYS[2], 120000)
local wait = 0
if rpm > 0 and req < 1 then wait = math.max(wait, (1 - req) * 60000 / rpm) end
if tpm > 0 and tok <= 0 then wait = math.max(wait, (1 - tok) * 60000 / tpm) end
local first = redis.call('ZRANGE', KEYS[1], 0, 0)[1]
if first ~= ARGV[1] then
"""
    + _SAVE
    + """
  return -1
end
if wait > 0 then
"""
    + _SAVE
    + """
  return math.max(1, math.ceil(wait))
end
if rpm > 0 then req = req - 1 end
"""
    + _SAVE
    + """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 0
"""
)

# KEYS: as `_ACQUIRE`'s. ARGV: unused, unused, the tokens used, the limits.
_SPEND = (
    _REFILL
    + """
if tpm > 0 then tok = tok - tonumber(ARGV[3]) end
"""
    + _SAVE
    + """
return 0
"""
)

# Which lane's turn it is, by the order claims serve them: lower first.
_PRIORITY = {WorkLane.interactive: 0, WorkLane.background: 1, WorkLane.sources: 2}
_lane: ContextVar[WorkLane] = ContextVar("model_rate_lane", default=WorkLane.background)


def priority_of(lane: WorkLane) -> int:
    return _PRIORITY[lane]


def priority() -> int:
    """The priority of calls made here: the lane of the run service item they're made for."""
    return priority_of(_lane.get())


@contextlib.contextmanager
def serving(lane: WorkLane) -> Iterator[None]:
    """Calls made within (and in tasks started within) are for an item of `lane`."""
    token = _lane.set(lane)
    try:
        yield
    finally:
        _lane.reset(token)


def limit_of(model: str) -> ModelRateLimit | None:
    """The model's limit, if one is set (and limits anything)."""
    limit = get_settings().model_rate_limits.get(model)
    if limit is None or not (limit.requests_per_minute or limit.tokens_per_minute):
        return None
    return limit


def _keys(model: str) -> list[str]:
    return [f"{PREFIX}{model}:waiting", f"{PREFIX}{model}:asked", f"{PREFIX}{model}:bucket"]


def _limits(limit: ModelRateLimit) -> list[int]:
    return [limit.requests_per_minute or 0, limit.tokens_per_minute or 0]


def _client() -> aioredis.Redis:
    return aioredis.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=1, socket_timeout=2
    )


async def acquire(model: str) -> None:
    """Wait until a call to `model` may be made within its limit, and take a request for it.
    At once if it has no limit."""
    limit = limit_of(model)
    if limit is None:
        return
    waiter = uuid.uuid4().hex
    # People in chat first, then the oldest: as claims are served.
    score = priority() * 10**13 + int(time.time() * 1000)
    client = _client()
    try:
        script = client.register_script(_ACQUIRE)
        args = [waiter, score, STALE_MS, *_limits(limit)]
        while True:
            wait = int(await script(keys=_keys(model), args=args))
            if wait == 0:
                return
            await asyncio.sleep(POLL if wait < 0 else min(wait / 1000, MAX_WAIT))
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't pace a call to %s; it goes ahead: %s", model, exc)
    finally:
        # Gone however it ended (cancelled while waiting, say), so it holds up no one.
        with contextlib.suppress(redis.RedisError, OSError, asyncio.CancelledError):
            waiting, asked, _ = _keys(model)
            await client.zrem(waiting, waiter)
            await client.zrem(asked, waiter)
        with contextlib.suppress(redis.RedisError, OSError):
            await client.aclose()


async def spend(model: str, tokens: int) -> None:
    """Take the tokens a call to `model` used from its bucket. Nothing if it has no limit on
    tokens."""
    limit = limit_of(model)
    if limit is None or not limit.tokens_per_minute or tokens <= 0:
        return
    client = _client()
    try:
        script = client.register_script(_SPEND)
        await script(keys=_keys(model), args=["", 0, tokens, *_limits(limit)])
    except (redis.RedisError, OSError) as exc:
        log.warning("couldn't count a call's tokens against %s's limit: %s", model, exc)
    finally:
        with contextlib.suppress(redis.RedisError, OSError):
            await client.aclose()


def requests_left(model: str) -> float:
    """What's left of the model's request bucket, as last written (for tests and metrics)."""
    with redis.Redis.from_url(get_settings().redis_url) as client:
        left = client.hget(_keys(model)[2], "req")
    return float(left) if left is not None else float("inf")


def reset() -> None:
    """Forget every model's bucket and waiters (for tests)."""
    with redis.Redis.from_url(get_settings().redis_url) as client:
        keys = client.keys(f"{PREFIX}*")
        if keys:
            client.delete(*keys)


def tokens_of(message: AIMessage | None) -> int:
    """The tokens a model's response says its call used, input and output."""
    usage = getattr(message, "usage_metadata", None) or {}
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


class RateLimitMiddleware(AgentMiddleware):
    """On every agent (`aios.runner.build_agent`): each model call waits for its model's limit
    (`acquire`), and its tokens are counted against it once it's answered (`spend`)."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        await acquire(self.model)
        response = await handler(request)
        used = sum(tokens_of(m) for m in response.result if isinstance(m, AIMessage))
        await spend(self.model, used)
        return response
