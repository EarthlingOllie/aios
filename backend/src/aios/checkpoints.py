"""Agent run history, kept by LangGraph's Postgres checkpointer: one thread per run,
`agent-run-<id>`, in the run's organisation's store, beside the run (`aios.datastores.models`).

Each store has its own checkpointer, its tables in the store's `langgraph` schema. The store's
roles can't create anything, so its setup runs as the store's owner, and before the store's
first use: from the store tree's revision 0010, and again each time the store is migrated
(`set_up_store`, from `aios.datastores.provision.upgrade_store`), so a newer LangGraph's
migrations reach every store with the deploy that brings it.

The checkpointer connects to the store as its read-write role, through the pooler when one is
configured (`aios.datastores.store_conninfo`), leaving no session state behind: no prepared
statements, and nothing set at connection startup. The search path that finds its tables is the
role's default in the store (`aios.datastores.provision.upgrade_store`). Like any table of the
store's, they're readable by its read-only role: conversations are the organisation's.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import psycopg
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios import turn_lease
from aios.datastores.connections import store_conninfo
from aios.turn_lease import Lease, LeaseLost, RunBusy, RunGone

__all__ = ["Lease", "LeaseLost", "RunBusy", "RunGone"]

# Where the checkpointer's tables are in each store; the runs are in `aios`.
SCHEMA = "langgraph"
TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# No session state, so the connections work through a pooler in transaction mode.
_CONNECT_KWARGS = {
    "autocommit": True,
    "prepare_threshold": None,
    "row_factory": dict_row,
}


def thread_id(run_id: int) -> str:
    """The run's thread, in its store: ids are the store's own, so the name needn't say whose."""
    return f"agent-run-{run_id}"


def thread_config(run_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(run_id)}}


# LangGraph builds its indexes CONCURRENTLY, which can't run inside a transaction; the setup runs
# in the migration's, so they're built plainly (on tables still empty, the first time).
MIGRATIONS = [m.replace(" CONCURRENTLY", "") for m in PostgresSaver.MIGRATIONS]


def set_up_store(conn: psycopg.Connection) -> None:
    """Create the checkpointer's schema and tables in a store, or bring them up to this
    LangGraph's, as the store's owner (`conn`); in the caller's transaction. Does nothing once
    they're up to date."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        # LangGraph's statements name its tables unqualified: in its schema, for this transaction.
        was = cur.execute("SELECT current_setting('search_path') AS path").fetchone()["path"]
        cur.execute(f"SET LOCAL search_path TO {SCHEMA}")
        cur.execute(MIGRATIONS[0])
        row = cur.execute("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1").fetchone()
        version = -1 if row is None else row["v"]
        for v in range(version + 1, len(MIGRATIONS)):
            cur.execute(MIGRATIONS[v])
            cur.execute("INSERT INTO checkpoint_migrations (v) VALUES (%s)", (v,))
        cur.execute("SELECT set_config('search_path', %s, true)", (was,))


# Whether the lease still holds, in the write's transaction: the row stays locked (shared) until
# the write commits, so stopping the turn (which clears the token), or deleting the run, waits for
# a write underway.
_FENCE = (
    "SELECT 1 FROM aios.agent_runs WHERE id = %s AND lease_token = %s"
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
async def turn(
    data_store_id: uuid.UUID, run_id: int, lease: Lease | None = None
) -> AsyncIterator[FencedSaver]:
    """A checkpointer for one turn of a run in the store `data_store_id`, on a connection of its
    own, holding the run's turn lease (`aios.turn_lease`) until the turn ends: turns of the same
    run (two tabs, two replicas) take turns, each starting from the history the previous one
    left.

    `lease` is one the caller already took (the run service, to hand a busy run's turn back);
    otherwise this waits for it. Raises `RunBusy` if the run stays busy past the wait, and
    `RunGone` if it has been deleted (its thread has gone with it, so nothing written now would
    be kept). One deleted or stopped later ends the turn (`stop_turns`)."""
    if lease is None:
        lease = await turn_lease.acquire(data_store_id, run_id)
    async with turn_lease.held(lease):
        conninfo = await asyncio.to_thread(store_conninfo, data_store_id)
        conn = await AsyncConnection.connect(conninfo, **_CONNECT_KWARGS)
        async with conn:
            yield FencedSaver(conn, lease)


def stop_turns(store: Session, data_store_id: uuid.UUID, run_ids: list[int]) -> None:
    """End the turns underway of these runs of the store (`store`, a session on it) wherever
    they run: see `turn_lease.stop`. Commits."""
    turn_lease.stop(store, data_store_id, run_ids)


def turns_underway(store: Session, run_ids: list[int]) -> set[int]:
    """Those of these runs (in the store `store` is a session on) with a turn underway, in any
    process: their lease is held."""
    return turn_lease.underway(store, run_ids)


@contextmanager
def sync_saver(data_store_id: uuid.UUID) -> Iterator[PostgresSaver]:
    """A checkpointer on the store outside a turn, on a connection of its own (e.g. to read many
    runs' histories on one)."""
    with Connection.connect(store_conninfo(data_store_id), **_CONNECT_KWARGS) as conn:
        yield PostgresSaver(conn)


def history(
    data_store_id: uuid.UUID, run_id: int, saver: PostgresSaver | None = None
) -> list[BaseMessage]:
    """The conversation of the store's run as of its latest checkpoint (empty before its first
    turn); read with `saver` (see `sync_saver`), or on a connection of its own."""
    if saver is None:
        with sync_saver(data_store_id) as saver:
            return history(data_store_id, run_id, saver)
    checkpoint = saver.get_tuple(thread_config(run_id))
    if checkpoint is None:
        return []
    return list(checkpoint.checkpoint["channel_values"].get("messages", []))


def delete_threads(store: Session, run_ids: list[int]) -> None:
    """Delete the runs' threads, in the transaction of `store` (a session on their store) that
    deletes the runs: a turn still running can't write the thread back, as its writes check its
    lease on the run's row (`FencedSaver`), and a write underway holds that row until it commits,
    so the deletion waits for it."""
    if not run_ids:
        return
    threads = [thread_id(run_id) for run_id in run_ids]
    for table in TABLES:
        store.execute(
            text(f"DELETE FROM {SCHEMA}.{table} WHERE thread_id = ANY(:threads)"),
            {"threads": threads},
        )


def messages_out(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """The messages as JSON: LangChain's own fields (type, content, tool calls, usage, ...)."""
    return [m.model_dump(mode="json") for m in messages]
