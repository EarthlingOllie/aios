from datetime import UTC, datetime

import pytest

from aios.cron import CronError, describe, next_run, next_runs, normalize


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def runs(expr: str, after: datetime, tz: str = "UTC", count: int = 3) -> list[datetime]:
    return next_runs(expr, tz, after, count)


def test_runs_strictly_after_and_in_utc():
    assert runs("*/15 9-10 * * *", utc(2026, 1, 1, 10, 30)) == [
        utc(2026, 1, 1, 10, 45),
        utc(2026, 1, 2, 9, 0),
        utc(2026, 1, 2, 9, 15),
    ]
    assert next_run("0 9 * * *", "UTC", utc(2026, 1, 1, 9)) == utc(2026, 1, 2, 9)
    assert all(r.tzinfo is UTC for r in runs("0 9 * * *", utc(2026, 1, 1), "Asia/Tokyo"))


def test_day_of_month_and_day_of_week_match_either_when_both_set():
    # The 13th, or any Friday (standard cron). 2026-02-06 and 2026-02-20 are Fridays.
    assert runs("0 0 13 * fri", utc(2026, 2, 1)) == [
        utc(2026, 2, 6),
        utc(2026, 2, 13),
        utc(2026, 2, 20),
    ]


def test_runs_in_the_schedules_timezone():
    # 09:00 in London is 09:00 UTC in winter and 08:00 UTC in summer.
    assert runs("0 9 * * *", utc(2026, 1, 1), "Europe/London", 1) == [utc(2026, 1, 1, 9)]
    assert runs("0 9 * * *", utc(2026, 7, 1), "Europe/London", 1) == [utc(2026, 7, 1, 8)]
    assert runs("0 9 * * *", utc(2026, 7, 1), "America/New_York", 1) == [utc(2026, 7, 1, 13)]


def test_daylight_saving_runs_each_day_once():
    # London springs forward at 01:00 UTC on 2026-03-29, skipping 01:30 local: that day's run
    # happens when the clock jumps, as cron does.
    assert runs("30 1 * * *", utc(2026, 3, 28, 2), "Europe/London") == [
        utc(2026, 3, 29, 1, 0),
        utc(2026, 3, 30, 0, 30),
        utc(2026, 3, 31, 0, 30),
    ]
    # It falls back at 01:00 UTC on 2026-10-25, so 01:30 local happens twice; it runs once.
    assert runs("30 1 * * *", utc(2026, 10, 24, 12), "Europe/London") == [
        utc(2026, 10, 25, 0, 30),
        utc(2026, 10, 26, 1, 30),
        utc(2026, 10, 27, 1, 30),
    ]


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "*/0 * * * *",
        "* * * foo *",
        "@sometimes",
        "0 0 30 2 *",
    ],
)
def test_invalid_expressions(expr):
    with pytest.raises(CronError, match="Invalid schedule"):
        next_runs(expr, "UTC", utc(2026, 1, 1), 1)


def test_invalid_timezone():
    for tz in ["Mars/Olympus_Mons", "", "../etc/passwd"]:
        with pytest.raises(CronError, match="Unknown timezone"):
            next_runs("* * * * *", tz, utc(2026, 1, 1), 1)


def test_normalize_and_describe():
    assert normalize("  0   9 * *\t1 ") == "0 9 * * 1"
    assert normalize(" @Daily ") == "@daily"
    assert describe("0 9 * * 1") == "At 09:00 on Monday"
    assert describe("*/15 * * * *") == "Every 15th minute of every hour"
