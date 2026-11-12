"""Insights: the panels every member sees on the Insights page (`aios.api.insights`).

Each panel is fixed SQL written here, never by a model, over the store's metrics-only
`insights` views (the store tree's `0030`), read through the read-only path
(`read_only_session`: the store's `ro` role, in a `READ ONLY` transaction that's rolled back),
for a range of days (UTC, both ends included) and, where a panel groups, a grouping:

- `activity`: turns and workflow runs by day, with how many succeeded and failed;
- `usage`: turns, tokens, tool calls and estimated cost in GBP, and the most used agents and
  tools;
- `workflows`: the slowest workflows and nodes, without the time they waited for approvals, and
  where runs fail, with each failing node's latest failed run;
- `approvals`: by kind, how many were approved, rejected, expired (apart) or are still waiting,
  and how long they waited;
- `chats`: by chat, messages, mentions and how the runs they started went;
- `storage`: the store's usage samples (`aios.datastores.quota`) against its caps;
- `sources`: each source's health, as the Sources tab shows it (`aios.api.sources`).

Turns from before turns were recorded count, with their counters null ("not tracked"), never
zero. Each panel's result is cached per store, panel, range and grouping for
`insights_cache_seconds` (`cached`).

Names (agents', workflows') come from the views; people's are the API's to add, as only it knows
who may see a per-person grouping.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.db import SessionLocal
from aios.models import DataStore, StoreUsageSample

Panel = Literal["activity", "usage", "workflows", "approvals", "chats", "storage", "sources"]
Group = Literal["agent", "workflow", "cause", "person"]

# How many rows a ranked list (slowest, most used, busiest) keeps.
TOP = 10

# What each grouping groups by, in the views that have it; NULL where a view hasn't (workflow
# runs have no agent).
_KEYS: dict[str, dict[str, str]] = {
    "turns": {
        "agent": "agent_id",
        "workflow": "workflow_id",
        "cause": "cause",
        "person": "person_id",
    },
    "workflow_runs": {
        "agent": "NULL::integer",
        "workflow": "workflow_id",
        "cause": "cause",
        "person": "person_id",
    },
    "usage": {
        "agent": "agent_id",
        "workflow": "workflow_id",
        "cause": "cause",
        "person": "person_id",
    },
}


@contextmanager
def read_only_session(data_store_id: uuid.UUID) -> Iterator[Session]:
    """A session on the store as its `ro` role, in a `READ ONLY` transaction rolled back on
    exit: the read-only path, for fixed SQL."""
    with store_session(data_store_id, "ro") as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield session
        finally:
            session.rollback()


# --- Caching -------------------------------------------------------------------------------------

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def cached(key: tuple, compute: Callable[[], Any]) -> Any:
    """`compute()`'s result, kept for `insights_cache_seconds` under `key`."""
    ttl = get_settings().insights_cache_seconds
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = compute()
    if ttl > 0:
        with _cache_lock:
            # Drop what's expired as we go, so the cache can't grow without end.
            for k in [k for k, (until, _) in _cache.items() if until <= now]:
                del _cache[k]
            _cache[key] = (now + ttl, value)
    return value


def panel(
    data_store_id: uuid.UUID, name: Panel, start: date, end: date, group: Group = "agent"
) -> dict:
    """The panel's data for the range (and grouping, for the panels that group), cached."""
    grouped = name in ("activity", "usage")
    key = (data_store_id, name, start, end, group if grouped else None)
    if name == "storage":
        return cached(key, lambda: storage(data_store_id, start, end))
    if name == "sources":
        return cached(key, lambda: sources(data_store_id))
    compute = PANELS[name]

    def run() -> dict:
        with read_only_session(data_store_id) as store:
            return (
                compute(store, *_bounds(start, end), group)
                if grouped
                else compute(store, *_bounds(start, end))
            )

    return cached(key, run)


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """The range as instants: from the start of `start` to the end of `end` (UTC)."""
    return (
        datetime.combine(start, dtime(), UTC),
        datetime.combine(end + timedelta(days=1), dtime(), UTC),
    )


def _rows(store: Session, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in store.execute(text(sql), params)]


def _names(store: Session, group: str, keys: list) -> dict:
    """The agents' or workflows' names, by id."""
    keys = [k for k in keys if k is not None]
    if group not in ("agent", "workflow") or not keys:
        return {}
    view, column = ("agents", "agent_id") if group == "agent" else ("workflows", "workflow_id")
    rows = store.execute(
        text(f"SELECT {column}, name FROM insights.{view} WHERE {column} = ANY(:keys)"),
        {"keys": keys},
    )
    return dict(rows.all())


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


# --- Activity ------------------------------------------------------------------------------------


def _series(rows: list[dict], names: dict) -> list[dict]:
    """Rows of (key, day, counts) as one series per key, its days in order."""
    series: dict = {}
    for row in rows:
        key = row.pop("key")
        entry = series.setdefault(key, {"key": key, "name": names.get(key), "days": []})
        row["day"] = row["day"].isoformat()
        entry["days"].append(row)
    return list(series.values())


def activity(store: Session, start: datetime, end: datetime, group: Group) -> dict:
    """Turns and workflow runs by day, grouped: how many, how many succeeded and failed, and
    (turns) how many aren't tracked."""
    turns = _rows(
        store,
        f"""
        SELECT {_KEYS["turns"][group]} AS key,
               (started_at AT TIME ZONE 'UTC')::date AS day,
               count(*) AS total,
               count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE NOT tracked) AS not_tracked
        FROM insights.turns
        WHERE started_at >= :start AND started_at < :end
        GROUP BY 1, 2 ORDER BY 2, 1
        """,
        start=start,
        end=end,
    )
    runs = _rows(
        store,
        f"""
        SELECT {_KEYS["workflow_runs"][group]} AS key,
               (created_at AT TIME ZONE 'UTC')::date AS day,
               count(*) AS total,
               count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               0 AS not_tracked
        FROM insights.workflow_runs
        WHERE created_at >= :start AND created_at < :end
        GROUP BY 1, 2 ORDER BY 2, 1
        """,
        start=start,
        end=end,
    )
    names = _names(store, group, [r["key"] for r in turns + runs])
    return {"turns": _series(turns, names), "workflow_runs": _series(runs, names)}


# --- Usage ---------------------------------------------------------------------------------------


def usage(store: Session, start: datetime, end: datetime, group: Group) -> dict:
    """By group: turns (and how many aren't tracked), their tokens and tool calls (tracked turns
    only: null when none is), and the ledger's estimated cost in GBP; and the most used agents
    (by turns) and tools (by calls)."""
    rows = _rows(
        store,
        f"""
        WITH t AS (
            SELECT {_KEYS["turns"][group]} AS key,
                   count(*) AS turns,
                   count(*) FILTER (WHERE NOT tracked) AS not_tracked,
                   sum(model_calls) AS model_calls,
                   sum(tool_calls) AS tool_calls,
                   sum(input_tokens) AS input_tokens,
                   sum(output_tokens) AS output_tokens
            FROM insights.turns
            WHERE started_at >= :start AND started_at < :end
            GROUP BY 1
        ), u AS (
            SELECT {_KEYS["usage"][group]} AS key,
                   sum(cost) AS cost,
                   count(*) FILTER (WHERE cost IS NULL) AS unpriced
            FROM insights.usage
            WHERE at >= :start AND at < :end
            GROUP BY 1
        ), k AS (SELECT key FROM t UNION SELECT key FROM u)
        SELECT k.key,
               coalesce(t.turns, 0) AS turns,
               coalesce(t.not_tracked, 0) AS not_tracked,
               t.model_calls, t.tool_calls, t.input_tokens, t.output_tokens,
               u.cost, coalesce(u.unpriced, 0) AS unpriced
        FROM k
        LEFT JOIN t ON t.key IS NOT DISTINCT FROM k.key
        LEFT JOIN u ON u.key IS NOT DISTINCT FROM k.key
        ORDER BY u.cost DESC NULLS LAST, turns DESC, k.key
        """,
        start=start,
        end=end,
    )
    names = _names(store, group, [r["key"] for r in rows])
    for row in rows:
        row["name"] = names.get(row["key"])
        row["cost"] = _number(row["cost"])
    top_agents = _rows(
        store,
        """
        SELECT t.agent_id AS key, a.name, count(*) AS turns
        FROM insights.turns t JOIN insights.agents a ON a.agent_id = t.agent_id
        WHERE t.started_at >= :start AND t.started_at < :end
        GROUP BY 1, 2 ORDER BY turns DESC, a.name LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP,
    )
    top_tools = _rows(
        store,
        """
        SELECT tool, sum(calls)::bigint AS calls
        FROM insights.tool_calls
        WHERE started_at >= :start AND started_at < :end
        GROUP BY tool ORDER BY calls DESC, tool LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP,
    )
    return {"currency": "GBP", "rows": rows, "top_agents": top_agents, "top_tools": top_tools}


# --- Workflows -----------------------------------------------------------------------------------


def workflows(store: Session, start: datetime, end: datetime) -> dict:
    """The slowest workflows (finished runs) and nodes, by their mean time running without
    waiting for approvals; and the nodes runs fail at, with each one's latest failed run.
    Subworkflow nodes are left out of nodes: their own nodes are there."""
    slowest = _rows(
        store,
        """
        SELECT r.workflow_id, w.name, count(*) AS runs,
               avg(r.seconds) AS mean_seconds, max(r.seconds) AS max_seconds,
               sum(r.waited_seconds) AS waited_seconds
        FROM insights.workflow_runs r JOIN insights.workflows w USING (workflow_id)
        WHERE r.created_at >= :start AND r.created_at < :end AND r.seconds IS NOT NULL
        GROUP BY 1, 2 ORDER BY mean_seconds DESC, w.name LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP,
    )
    nodes = _rows(
        store,
        """
        SELECT s.workflow_id, w.name, s.node, s.kind, count(*) AS runs,
               avg(s.seconds) AS mean_seconds, max(s.seconds) AS max_seconds
        FROM insights.workflow_steps s JOIN insights.workflows w USING (workflow_id)
        WHERE s.started_at >= :start AND s.started_at < :end AND s.seconds IS NOT NULL
          AND s.kind <> 'workflow' AND s.status IN ('succeeded', 'failed')
        GROUP BY 1, 2, 3, 4 ORDER BY mean_seconds DESC, w.name, s.node LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP,
    )
    failures = _rows(
        store,
        """
        SELECT s.workflow_id, w.name, s.node, s.kind, count(*) AS failures,
               max(s.finished_at) AS latest_at,
               (array_agg(s.run_id ORDER BY s.finished_at DESC NULLS LAST, s.step_id DESC))[1]
                   AS latest_run_id
        FROM insights.workflow_steps s JOIN insights.workflows w USING (workflow_id)
        WHERE s.started_at >= :start AND s.started_at < :end
          AND s.status = 'failed' AND s.kind <> 'workflow'
        GROUP BY 1, 2, 3, 4 ORDER BY failures DESC, latest_at DESC LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP,
    )
    for row in slowest + nodes:
        for column in ("mean_seconds", "max_seconds", "waited_seconds"):
            if column in row:
                row[column] = _number(row[column])
    return {"slowest_workflows": slowest, "slowest_nodes": nodes, "failures": failures}


# --- Approvals -----------------------------------------------------------------------------------

# Outcomes that count as approved: a schema change approved goes on to be applied, fail to
# apply, or go stale, but it was approved all the same.
_APPROVED = "('approved', 'applied', 'failed', 'stale')"


def approvals(store: Session, start: datetime, end: datetime) -> dict:
    """By kind, the approvals asked for in the range: how many were approved, rejected,
    cancelled, are waiting, and expired (apart: an expiry isn't a decision); and how long the
    decided ones waited."""
    rows = _rows(
        store,
        f"""
        SELECT kind, count(*) AS total,
               count(*) FILTER (WHERE status IN {_APPROVED}) AS approved,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(*) FILTER (WHERE status = 'expired') AS expired,
               count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
               count(*) FILTER (WHERE status = 'pending') AS pending,
               avg(wait_seconds) FILTER (WHERE status IN {_APPROVED} OR status = 'rejected')
                   AS mean_wait_seconds,
               max(wait_seconds) FILTER (WHERE status IN {_APPROVED} OR status = 'rejected')
                   AS max_wait_seconds
        FROM insights.approvals
        WHERE created_at >= :start AND created_at < :end
        GROUP BY kind ORDER BY kind
        """,
        start=start,
        end=end,
    )
    for row in rows:
        row["mean_wait_seconds"] = _number(row["mean_wait_seconds"])
        row["max_wait_seconds"] = _number(row["max_wait_seconds"])
    return {"kinds": rows}


# --- Chats ---------------------------------------------------------------------------------------


def chats(store: Session, start: datetime, end: datetime) -> dict:
    """The busiest chats in the range: people's messages, agents' and workflows' messages,
    mentions of agents and workflows, and how the runs those started went."""
    rows = _rows(
        store,
        """
        WITH m AS (
            SELECT chat_id,
                   count(*) FILTER (WHERE author_kind = 'user') AS messages,
                   count(*) FILTER (WHERE author_kind IN ('agent', 'workflow'))
                       AS agent_messages
            FROM insights.chat_messages
            WHERE kind = 'text' AND author_kind <> 'system'
              AND created_at >= :start AND created_at < :end
            GROUP BY 1
        ), i AS (
            SELECT chat_id, count(*) AS mentions,
                   count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   count(*) FILTER (WHERE status = 'refused') AS refused,
                   count(*) FILTER (WHERE status NOT IN ('succeeded', 'failed', 'refused'))
                       AS in_progress
            FROM insights.mentions
            WHERE created_at >= :start AND created_at < :end
            GROUP BY 1
        )
        SELECT c.chat_id, c.name, c.archived_at IS NOT NULL AS archived,
               coalesce(m.messages, 0) AS messages,
               coalesce(m.agent_messages, 0) AS agent_messages,
               coalesce(i.mentions, 0) AS mentions,
               coalesce(i.succeeded, 0) AS succeeded, coalesce(i.failed, 0) AS failed,
               coalesce(i.refused, 0) AS refused, coalesce(i.in_progress, 0) AS in_progress
        FROM insights.chats c
        LEFT JOIN m USING (chat_id) LEFT JOIN i USING (chat_id)
        WHERE m.chat_id IS NOT NULL OR i.chat_id IS NOT NULL
        ORDER BY messages DESC, mentions DESC, agent_messages DESC, c.name LIMIT :top
        """,
        start=start,
        end=end,
        top=TOP * 2,
    )
    return {"chats": rows}


# --- Storage -------------------------------------------------------------------------------------


def storage(data_store_id: uuid.UUID, start: date, end: date) -> dict:
    """The store's usage samples in the range (the platform's: one per measurement), oldest
    first, against its caps."""
    since, until = _bounds(start, end)
    settings = get_settings()
    with SessionLocal() as db:
        samples = db.execute(
            select(
                StoreUsageSample.measured_at,
                StoreUsageSample.db_bytes,
                StoreUsageSample.files_bytes,
            )
            .where(
                StoreUsageSample.data_store_id == data_store_id,
                StoreUsageSample.measured_at >= since,
                StoreUsageSample.measured_at < until,
            )
            .order_by(StoreUsageSample.measured_at)
        ).all()
        current = db.execute(
            select(DataStore.db_bytes, DataStore.files_bytes, DataStore.usage_measured_at).where(
                DataStore.id == data_store_id
            )
        ).one()
    return {
        "db_quota_bytes": settings.data_store_db_quota_bytes,
        "files_quota_bytes": settings.data_store_files_quota_bytes,
        "db_bytes": current.db_bytes,
        "files_bytes": current.files_bytes,
        "measured_at": current.usage_measured_at,
        "samples": [
            {"measured_at": s.measured_at, "db_bytes": s.db_bytes, "files_bytes": s.files_bytes}
            for s in samples
        ],
    }


# --- Sources -------------------------------------------------------------------------------------


def sources(data_store_id: uuid.UUID) -> dict:
    """Each source but Uploads, with its health and last sync as the Sources tab shows them;
    `shown` only once there is one."""
    from aios.api.sources import sources_out
    from aios.datastores.models import SourceKind

    with read_only_session(data_store_id) as store:
        rows = [s for s in sources_out(store) if s.kind != SourceKind.upload]
    return {"shown": bool(rows), "sources": [s.model_dump(mode="json") for s in rows]}


PANELS: dict[str, Callable[..., dict]] = {
    "activity": activity,
    "usage": usage,
    "workflows": workflows,
    "approvals": approvals,
    "chats": chats,
}
