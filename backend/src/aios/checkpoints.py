"""Agent run history, kept by LangGraph's Postgres checkpointer: one thread per run.

The checkpointer's tables live in their own schema (`langgraph`), which LangGraph creates and
migrates itself (`setup`); Alembic manages the `public` schema only and doesn't see them,
except that downgrading past 0005 drops the schema along with the runs its threads belong to.
"""

import asyncio
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
from sqlalchemy.orm import Session

from aios import turn_lease
from aios.config import get_settings, libpq_conninfo
from aios.turn_lease import Lease, LeaseLost, RunBusy, RunGone

__all__ = ["Lease", "LeaseLost", "RunBusy", "RunGone"]

SCHEMA = "langgraph"

# The setup's lock key (the first of the two-int form of a transaction's advisory lock).
_SETUP_LOCK = 0x6C67

_ready = False


def conninfo() -> str:
    """DATABASE_URL as plain libpq connection info (it's written for SQLAlchemy)."""
    return libpq_conninfo(get_settings().database_url)


# No session state, so the connections work through a pooler in transaction mode: no prepared
# statements, and nothing set at connection startup. The search path that finds the
# checkpointer's tables is the app's role's default on the database (migration 0056).
_CONNECT_KWARGS = {
    "autocommit": True,
    "prepare_threshold": None,
    "row_factory": dict_row,
}


def thread_id(run_id: int) -> str:
    return f"agent-run-{run_id}"


def thread_config(run_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(run_id)}}


class _SetupSaver(AsyncPostgresSaver):
    # LangGraph builds its indexes CONCURRENTLY, which can't run inside a transaction; the setup
    # runs in one, so the indexes are built plainly (only ever on the first setup, on tables
    # still empty, since each migration runs once).
    MIGRATIONS = [m.replace(" CONCURRENTLY", "") for m in AsyncPostgresSaver.MIGRATIONS]


async def setup(conn: AsyncConnection) -> None:
    """Create or migrate the checkpointer's tables, once per process (it's idempotent).

    In one transaction, under a lock that lasts only as long as it, so replicas starting
    together don't migrate at the same time."""
    global _ready
    if _ready:
        return
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (_SETUP_LOCK,))
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        # Where its tables are created: the role's default puts `public` first.
        await conn.execute(f"SET LOCAL search_path TO {SCHEMA}")
        await _SetupSaver(conn).setup()
    _ready = True


async def ensure_ready() -> None:
    """Set up the checkpointer's tables now, if this process hasn't yet, rather than in its
    first turn (see `setup`)."""
    if _ready:
        return
    conn = await AsyncConnection.connect(conninfo(), **_CONNECT_KWARGS)
    async with conn:
        await setup(conn)


# Whether the lease still holds, in the write's transaction: the row stays locked (shared) until
# the write commits, so stopping the turn (which clears the token) waits for a write underway.
_FENCE = (
    "SELECT 1 FROM public.agent_runs WHERE id = %s AND lease_token = %s"
    " AND lease_until > now() FOR SHARE"
)


class FencedSaver(AsyncPostgresSaver):
    """The checkpointer of a turn holding `lease`: every write first checks, in its own
    transaction, that the lease still holds, and fails (`LeaseLost`) storing nothing if not."""

    def __init__(self, conn: AsyncConnection, lease: Lease) -> None:
        super().__init__(conn)
        self.lease = lease
        self._fence_lock = asyncio.Lock()

    @asynccontextmanager
    async def _fenced(self) -> AsyncIterator[None]:
        async with self._fence_lock, self.conn.transaction():
            cursor = await self.conn.execute(_FENCE, (self.lease.run_id, self.lease.token))
            if await cursor.fetchone() is None:
                raise LeaseLost(f"Agent run {self.lease.run_id}: this turn's lease has lapsed")
            yield

    async def aput(self, config, checkpoint, metadata, new_versions):
        async with self._fenced():
            return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        async with self._fenced():
            return await super().aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._fenced():
            return await super().adelete_thread(thread_id)


@asynccontextmanager
async def turn(run_id: int, lease: Lease | None = None) -> AsyncIterator[FencedSaver]:
    """A checkpointer for one turn of a run, on a connection of its own, holding the run's turn
    lease (`aios.turn_lease`) until the turn ends: turns of the same run (two tabs, two replicas)
    take turns, each starting from the history the previous one left.

    `lease` is one the caller already took (the chat endpoint, to answer 409 before streaming);
    otherwise this waits for it. Raises `RunBusy` if the run stays busy past the wait, and
    `RunGone` if it has been deleted (its thread has been deleted, or will be, so nothing
    written now would be kept). One deleted or stopped later ends the turn (`stop_turns`)."""
    await ensure_ready()
    if lease is None:
        lease = await turn_lease.acquire(run_id)
    async with turn_lease.held(lease):
        conn = await AsyncConnection.connect(conninfo(), **_CONNECT_KWARGS)
        async with conn:
            yield FencedSaver(conn, lease)


def stop_turns(db: Session, run_ids: list[int]) -> None:
    """End the turns underway of these runs (deleted, say), wherever they run: see
    `turn_lease.stop`. Commits."""
    turn_lease.stop(db, run_ids)


def turns_underway(db: Session, run_ids: list[int]) -> set[int]:
    """Those of these runs with a turn underway, in any process: their lease is held."""
    return turn_lease.underway(db, run_ids)


@contextmanager
def sync_saver() -> Iterator[PostgresSaver]:
    """A checkpointer outside a turn, on a connection of its own (e.g. to read many runs'
    histories on one)."""
    with Connection.connect(conninfo(), **_CONNECT_KWARGS) as conn:
        yield PostgresSaver(conn)


def history(run_id: int, saver: PostgresSaver | None = None) -> list[BaseMessage]:
    """The run's conversation as of its latest checkpoint (empty before its first turn); read
    with `saver` (see `sync_saver`), or on a connection of its own."""
    if saver is None:
        with sync_saver() as saver:
            return history(run_id, saver)
    try:
        checkpoint = saver.get_tuple(thread_config(run_id))
    except UndefinedTable:  # no turn has run anywhere yet (see `setup`)
        return []
    if checkpoint is None:
        return []
    return list(checkpoint.checkpoint["channel_values"].get("messages", []))


def delete_threads(run_ids: list[int]) -> None:
    """Delete the runs' checkpoints. They aren't tied to `agent_runs`, so they don't cascade.

    Only once the runs' deletion is committed: a turn still running can't write the thread back,
    as its writes check its lease on the run's row (`FencedSaver`), and a write underway holds
    that row until it commits, so the deletion waited for it."""
    if not run_ids:
        return
    with sync_saver() as saver:
        try:
            for run_id in run_ids:
                saver.delete_thread(thread_id(run_id))
        except UndefinedTable:
            pass


def messages_out(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The messages as JSON: LangChain's own fields (type, content, tool calls, usage, ...)."""
    return [m.model_dump(mode="json") for m in messages]
