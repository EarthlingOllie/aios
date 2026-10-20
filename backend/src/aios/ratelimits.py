"""Rate limits, counted in Redis so every API process shares them (the `limits` package, with a
moving window). An endpoint calls `throttle` for each count an attempt goes against, before doing
the work; past a limit it answers 429 with `Retry-After`. There's no lockout: an attempt that's
refused isn't counted, so the window frees up on its own."""

import hashlib
import math
import time
from functools import cache

from fastapi import HTTPException, Request, status
from limits import parse
from limits.storage import RedisStorage
from limits.strategies import MovingWindowRateLimiter

from aios.config import get_settings

TOO_MANY_ATTEMPTS = "Too many attempts. Try again later."


@cache
def _limiter() -> MovingWindowRateLimiter:
    return MovingWindowRateLimiter(
        RedisStorage(get_settings().redis_url, key_prefix="aios-rate-limits")
    )


def throttle(name: str, rate: str, key: str) -> None:
    """Count an attempt at `name` (e.g. "login-email") by `key` (an IP, a user id, or a
    `hashed` email) against `rate`, a `limits` string such as "10/15 minutes" (from settings).
    Past it, the attempt isn't counted and the request is refused: 429, with `Retry-After` saying
    when the oldest counted attempt leaves the window."""
    limit = parse(rate)
    limiter = _limiter()
    if limiter.hit(limit, name, key):
        return
    reset_at, _ = limiter.get_window_stats(limit, name, key)
    retry_after = max(1, math.ceil(reset_at - time.time()))
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        TOO_MANY_ATTEMPTS,
        headers={"Retry-After": str(retry_after)},
    )


def hashed(value: str) -> str:
    """`value` as a key that doesn't keep it (an email, say) in Redis."""
    return hashlib.sha256(value.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """The address the request came from. Behind `trusted_proxy_count` proxies (the load
    balancer; locally Compose's nginx), each of which appends the address it saw to
    X-Forwarded-For, that's the entry the outermost one appended: anything to its left was sent
    by the client, so can't be trusted."""
    hops = get_settings().trusted_proxy_count
    if hops > 0:
        forwarded = [
            ip.strip()
            for header in request.headers.getlist("x-forwarded-for")
            for ip in header.split(",")
        ]
        forwarded = [ip for ip in forwarded if ip]
        if forwarded:
            return forwarded[-min(hops, len(forwarded))]
    return request.client.host if request.client else "unknown"


def reset() -> None:
    """Forget every count (for tests)."""
    _limiter().storage.reset()
