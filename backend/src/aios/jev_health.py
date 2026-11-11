"""Jev's health: polled by the beat (`aios.worker.check_jev_health_task`, every
`jev_health_interval_seconds`), feeding an alert, metrics and the notice people see in the app.

- **Metrics.** Each poll writes a `JevUp` metric (1 or 0) in CloudWatch's embedded metric format,
  one JSON line on the log, which CloudWatch turns into the `aios/Jev` namespace's metric; the
  alarm on it (infra/terraform/jev.tf) is the alert.
- **Alert.** Going down, and coming back, are logged at ERROR and INFO (`ALERT: Jev is down`),
  once per change.
- **The notice.** The last poll is kept in Redis (`status`), which the API answers
  (`GET /api/jev-status`), so the app can say why documents wait and tools fail. A poll older than
  a few intervals counts as unknown, not up.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import redis

from aios import jev
from aios.config import get_settings

log = logging.getLogger(__name__)

KEY = "aios:jev:health"
# A poll this many intervals old no longer says anything.
STALE_AFTER_INTERVALS = 4


def _redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)


@dataclass(frozen=True)
class Health:
    # None: not known (never polled, or the last poll is stale).
    up: bool | None
    checked_at: float | None


def metric(up: bool) -> str:
    """The `JevUp` metric, as a CloudWatch embedded metric format log line."""
    return json.dumps(
        {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "aios/Jev",
                        "Dimensions": [[]],
                        "Metrics": [{"Name": "JevUp", "Unit": "Count"}],
                    }
                ],
            },
            "JevUp": 1 if up else 0,
        }
    )


def poll() -> Health:
    """Ask Jev whether it's up, and record (and report) the answer."""
    up = asyncio.run(jev.classifier().healthy())
    print(metric(up), flush=True)
    now = time.time()
    try:
        client = _redis()
        before = client.getset(KEY, json.dumps({"up": up, "checked_at": now}))
    except (redis.RedisError, OSError):
        log.exception("Couldn't record Jev's health")
        before = None
    was_up = json.loads(before)["up"] if before else None
    if not up and was_up is not False:
        log.error(
            "ALERT: Jev is down: documents wait unsearchable and checked tool results fail"
            " until it's back",
            extra={"alert": "jev_down"},
        )
    elif up and was_up is False:
        log.info("Jev is back up", extra={"alert": "jev_up"})
    return Health(up=up, checked_at=now)


def status() -> Health:
    """Jev's health as last polled; unknown if it hasn't been lately."""
    try:
        raw = _redis().get(KEY)
    except (redis.RedisError, OSError):
        return Health(up=None, checked_at=None)
    if not raw:
        return Health(up=None, checked_at=None)
    last = json.loads(raw)
    stale = STALE_AFTER_INTERVALS * get_settings().jev_health_interval_seconds
    if time.time() - last["checked_at"] > stale:
        return Health(up=None, checked_at=last["checked_at"])
    return Health(up=bool(last["up"]), checked_at=last["checked_at"])
