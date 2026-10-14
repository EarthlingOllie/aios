"""Cron expressions for schedule triggers: standard 5-field cron, evaluated in a timezone.

Parsing and stepping are cronsim's (as cron: a day-of-month and a day-of-week that are both
restricted match either; across daylight saving, a local time the clock skips runs when it jumps,
and one it repeats runs once). Results are always in UTC, which is how they're stored.
"""

from datetime import UTC, datetime
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from cronsim import CronSim, CronSimError

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    pass


_TIMEZONES = frozenset(available_timezones() | {"UTC"})


def normalize(expr: str) -> str:
    """The expression as stored: single spaces, and macros in lower case."""
    expr = " ".join(expr.split())
    return expr.lower() if expr.startswith("@") else expr


def timezones() -> set[str]:
    """The IANA timezone names schedules can use."""
    return _TIMEZONES


def zone(name: str) -> ZoneInfo:
    # Checked against the list first: ZoneInfo would also take some paths and odd keys.
    if name not in _TIMEZONES:
        raise CronError(f"Unknown timezone: {name!r}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"Unknown timezone: {name!r}") from exc


def _sim(expr: str, tz: str, after: datetime) -> CronSim:
    local = after.astimezone(zone(tz))
    expanded = MACROS.get(normalize(expr), expr)
    try:
        return CronSim(expanded, local)
    except CronSimError as exc:
        raise CronError(f"Invalid schedule: {exc}") from exc


def next_runs(expr: str, tz: str, after: datetime, count: int) -> list[datetime]:
    """The next `count` times the schedule fires strictly after `after`, in UTC."""
    # cronsim rejects expressions that never fire (e.g. "0 0 30 2 *") when parsing them.
    return [t.astimezone(UTC) for t in islice(_sim(expr, tz, after), count)]


def next_run(expr: str, tz: str, after: datetime) -> datetime:
    return next_runs(expr, tz, after, 1)[0]


def describe(expr: str) -> str:
    """A plain-English description, e.g. "At 09:00 on Monday"; the expression if there's none."""
    try:
        return _sim(expr, "UTC", datetime.now(UTC)).explain()
    except CronError:
        return expr
