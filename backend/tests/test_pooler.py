"""Stores reached through the connection pooler (PgBouncer, in transaction mode): the app's store
connections carry no session state from one transaction to the next, so any server connection
the pooler hands them will do. The suite runs with `DATA_STORE_POOLER_ADDRESS` pointing at one in
CI (docs/testing.md), so every store test goes through it there."""

import asyncio

from conftest import store_of
from sqlalchemy import select, text

from aios import checkpoints
from aios.config import get_settings
from aios.datastores import connections, store_session
from aios.db import SessionLocal
from aios.models import DataStore


def test_store_sessions_are_reached_at_the_pooler_when_one_is_configured(db, user, monkeypatch):
    with SessionLocal() as session:
        store = session.scalar(select(DataStore).where(DataStore.id == store_of(user)))
    monkeypatch.setattr(get_settings(), "data_store_pooler_address", "pooler.internal:6432")
    url = connections.store_url(store, "rw")
    assert (url.host, url.port) == ("pooler.internal", 6432)
    assert url.database == store.dbname
    assert url.username == f"{store.role_prefix}_rw"

    monkeypatch.setattr(get_settings(), "data_store_pooler_address", "")
    direct = connections.store_url(store, "ro")
    assert direct.port != 6432


def test_frequently_run_store_queries_prepare_no_statements(db, user):
    store_id = store_of(user)
    for role in ("rw", "ro"):
        with store_session(store_id, role) as session:
            # One transaction, so it's one server connection even through the pooler.
            for _ in range(20):
                session.execute(text("SELECT count(*) FROM aios.documents WHERE id = :i"), {"i": 1})
            prepared = session.scalar(text("SELECT count(*) FROM pg_prepared_statements"))
            session.rollback()
        assert prepared == 0, role


def test_store_engines_pool_one_connection_and_four_more_while_busy(db, user):
    engine = connections.store_engine(store_of(user), "rw")
    assert (engine.pool.size(), engine.pool._max_overflow) == (1, 4)


def test_the_checkpointer_prepares_no_statements_and_sets_nothing_at_startup(db):
    async def check() -> tuple[int, str]:
        with checkpoints.sync_saver() as saver:
            for _ in range(20):
                checkpoints.history(0, saver)
            row = saver.conn.execute("SELECT count(*) AS n FROM pg_prepared_statements").fetchone()
            options = saver.conn.info.get_parameters().get("options", "")
        return row["n"], options

    prepared, options = asyncio.run(check())
    assert prepared == 0
    assert "search_path" not in options
