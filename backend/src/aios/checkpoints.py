"""Agent run history, kept by LangGraph's Postgres checkpointer: one thread per run.

The checkpointer's tables live in their own schema (`langgraph`), which LangGraph creates and
migrates itself (`setup`); Alembic manages the `public` schema only and doesn't see them,
except that downgrading past 0005 drops the schema along with the runs its threads belong to.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, Connection
from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios.config import get_settings, libpq_conninfo

SCHEMA = "langgraph"

# Advisory lock keys (the first of the two-int form; the second is the run id, or 0).
_SETUP_LOCK = 0x6C67
_TURN_LOCK = 0x7275

_ready = False


def conninfo() -> str:
    """DATABASE_URL as plain libpq connection info (it's written for SQLAlchemy)."""
    return libpq_conninfo(get_settings().database_url)


_CONNECT_KWARGS = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
    "options": f"-c search_path={SCHEMA}",
}


def thread_id(run_id: int) -> str:
    return f"agent-run-{run_id}"


def thread_config(run_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(run_id)}}


async def setup(conn: AsyncConnection) -> None:
    """Create or migrate the checkpointer's tables, once per process (it's idempotent).

    Under an advisory lock, so replicas starting together don't migrate at the same time.
    """
    global _ready
    if _ready:
        return
    await conn.execute("SELECT pg_advisory_lock(%s, 0)", (_SETUP_LOCK,))
    try:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        await AsyncPostgresSaver(conn).setup()
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s, 0)", (_SETUP_LOCK,))
    _ready = True


@asynccontextmanager
async def turn(run_id: int) -> AsyncIterator[AsyncPostgresSaver]:
    """A checkpointer for one turn of a run, on a connection of its own.

    The connection holds the run's turn lock until the turn ends, so turns of the same run (two
    tabs, two replicas) take turns: each starts from the history the previous one left, rather
    than both forking from the same checkpoint.
    """
    conn = await AsyncConnection.connect(conninfo(), **_CONNECT_KWARGS)
    async with conn:
        await setup(conn)
        await conn.execute("SELECT pg_advisory_lock(%s, %s)", (_TURN_LOCK, run_id))
        # Closing the connection releases the lock, however the turn ends.
        yield AsyncPostgresSaver(conn)


def turns_underway(db: Session, run_ids: list[int]) -> set[int]:
    """Those of these runs with a turn underway, in any process: their turn lock is held."""
    if not run_ids:
        return set()
    # The two-int form of an advisory lock shows its keys as classid and objid (objsubid 2).
    rows = db.execute(
        text(
            "SELECT objid::bigint FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND objsubid = 2 AND classid = CAST(:key AS oid)"
            " AND objid::bigint = ANY(:ids)"
        ),
        {"key": _TURN_LOCK, "ids": list(run_ids)},
    )
    return {row[0] for row in rows}


@contextmanager
def _sync_saver() -> Iterator[PostgresSaver]:
    with Connection.connect(conninfo(), **_CONNECT_KWARGS) as conn:
        yield PostgresSaver(conn)


def history(run_id: int) -> list[BaseMessage]:
    """The run's conversation as of its latest checkpoint (empty before its first turn)."""
    with _sync_saver() as saver:
        try:
            checkpoint = saver.get_tuple(thread_config(run_id))
        except UndefinedTable:  # no turn has run anywhere yet (see `setup`)
            return []
    if checkpoint is None:
        return []
    return list(checkpoint.checkpoint["channel_values"].get("messages", []))


def delete_threads(run_ids: list[int]) -> None:
    """Delete the runs' checkpoints. They aren't tied to `agent_runs`, so they don't cascade.

    Each under the run's turn lock: a turn still running would write the thread back.
    """
    if not run_ids:
        return
    with _sync_saver() as saver:
        try:
            for run_id in run_ids:
                saver.conn.execute("SELECT pg_advisory_lock(%s, %s)", (_TURN_LOCK, run_id))
                try:
                    saver.delete_thread(thread_id(run_id))
                finally:
                    saver.conn.execute("SELECT pg_advisory_unlock(%s, %s)", (_TURN_LOCK, run_id))
        except UndefinedTable:
            pass


def messages_out(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The messages as JSON: LangChain's own fields (type, content, tool calls, usage, ...)."""
    return [m.model_dump(mode="json") for m in messages]
