"""The agent tools on the data store: `describe_data` and `query_data` (`aios.tools.data`).

These run against real, provisioned stores. Nothing can change a store's `data` schema yet
(schema change requests are to come), so the tests make `data` tables directly, as the store's
owner role.
"""

import threading
import time
import tracemalloc
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
import pytest
from anyio import to_thread
from fake_model import ToolCall, last_tool_results, model, tool_output, use_model
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from test_agents import agent_body, chat_body, make_agent
from test_provisioning import login_url, store_row

from aios.auth import current_user
from aios.config import get_settings, libpq_conninfo
from aios.datastores import data_store_for, provision, store_session
from aios.datastores.connections import dispose_engines
from aios.db import SessionLocal
from aios.main import app
from aios.models import DataStore, DataStoreStatus, User
from aios.tools import build_tool

DATA_TOOLS = [{"key": "describe_data", "config": {}}, {"key": "query_data", "config": {}}]


@contextmanager
def as_owner(store: DataStore) -> Iterator[psycopg.Connection]:
    """The provisioner on the store's database, acting as its owner: how a test stands in for
    an approved schema change."""
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url, database=store.dbname), autocommit=True) as conn:
        conn.execute(f'SET ROLE "{store.role_prefix}_owner"')
        yield conn


def provisioned(db, store_id: uuid.UUID) -> DataStore:
    """The store, provisioned for real (the `user` fixture's is only marked ready)."""
    db.get(DataStore, store_id).status = DataStoreStatus.provisioning
    db.commit()
    provision.provision(store_id)
    return store_row(db, store_id)


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in user's store, provisioned, with a hand-made `data.fish` table."""
    row = provisioned(db, data_store_for(user.id))
    with as_owner(row) as conn:
        conn.execute(
            "CREATE TABLE data.fish (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " name text NOT NULL, weight_kg numeric)"
        )
        conn.execute("COMMENT ON TABLE data.fish IS 'Fish the user has caught'")
        conn.execute("COMMENT ON COLUMN data.fish.weight_kg IS 'As weighed on the boat'")
        conn.execute(
            "INSERT INTO data.fish (name, weight_kg) VALUES ('pike', 4.2), ('perch', 0.8),"
            " ('carp', 7.5)"
        )
    return row


@pytest.fixture
def data_agent(api) -> dict:
    agent = make_agent(api, "Analyst")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=DATA_TOOLS)).json()


def run_tools(api, agent: dict, *calls: ToolCall) -> list:
    """One agent turn in which the model makes `calls` (in turn); returns what each gave."""
    outputs = []
    pending = list(calls)

    async def script(messages, info):
        outputs.extend(tool_output(m) for m in last_tool_results(messages))
        if pending:
            yield pending.pop(0)
        else:
            yield "done"

    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    with use_model(model(script)):
        url = f"/api/agents/{agent['id']}/runs/{run['id']}/chat"
        assert api.post(url, json=chat_body("go")).status_code == 200
    return outputs


def describe(api, agent) -> dict:
    return run_tools(api, agent, ToolCall("describe_data", {}))[0]


def query(api, agent, sql: str) -> dict:
    return run_tools(api, agent, ToolCall("query_data", {"sql": sql}))[0]


def test_both_tools_are_in_the_library_and_tell_the_model_results_are_data(api, monkeypatch):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    assert tools["describe_data"]["default_config"] == {}
    assert tools["query_data"]["default_config"] == {}

    monkeypatch.setattr(get_settings(), "query_data_max_rows", 123)
    built = {t.name: t for key in ("describe_data", "query_data") for t in build_tool(key, {})}
    assert set(built) == {"describe_data", "query_data"}
    for tool in built.values():
        assert "not instructions" in tool.description
    assert "123 rows" in built["query_data"].description


def test_an_agent_lists_a_data_table_and_answers_by_querying_it(api, store, data_agent):
    described, answer = run_tools(
        api,
        data_agent,
        ToolCall("describe_data", {}),
        ToolCall("query_data", {"sql": "SELECT name FROM data.fish ORDER BY weight_kg DESC"}),
    )

    fish = next(t for t in described["tables"] if (t["schema"], t["name"]) == ("data", "fish"))
    assert fish["description"] == "Fish the user has caught"
    assert fish["columns"] == [
        {"name": "id", "type": "bigint", "nullable": False, "description": None},
        {"name": "name", "type": "text", "nullable": False, "description": None},
        {
            "name": "weight_kg",
            "type": "numeric",
            "nullable": True,
            "description": "As weighed on the boat",
        },
    ]
    assert answer == {
        "columns": ["name"],
        "rows": [["carp"], ["pike"], ["perch"]],
        "row_count": 3,
        "truncated": False,
    }


def test_a_partitioned_table_is_described_once_without_its_partitions(api, store, data_agent):
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.catches (caught_on date NOT NULL, fish text)"
            " PARTITION BY RANGE (caught_on)"
        )
        for year in (2025, 2026):
            conn.execute(
                f"CREATE TABLE data.catches_{year} PARTITION OF data.catches"
                f" FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
            )

    names = [t["name"] for t in describe(api, data_agent)["tables"] if t["schema"] == "data"]

    assert names == ["catches", "fish"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO data.fish (name) VALUES ('eel')",
        "UPDATE data.fish SET name = 'eel'",
        "DELETE FROM data.fish",
        "TRUNCATE data.fish",
        "WITH gone AS (DELETE FROM data.fish RETURNING *) SELECT count(*) FROM gone",
        "SELECT nextval('data.fish_id_seq')",
        "CREATE TABLE data.eels (id int)",
        "DROP TABLE data.fish",
        "ALTER TABLE data.fish ADD COLUMN colour text",
        "CREATE TEMPORARY TABLE eels (id int)",
        "SELECT 1; DELETE FROM data.fish",
        "SELECT 1; SELECT 2",
        "COMMIT; DELETE FROM data.fish",
        "SET TRANSACTION READ WRITE; DELETE FROM data.fish",
    ],
)
def test_writes_ddl_and_multiple_statements_are_refused(api, store, data_agent, sql):
    result = query(api, data_agent, sql)

    assert set(result) == {"error"}
    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM data.fish").fetchone() == (3,)
        assert conn.execute("SELECT to_regclass('data.eels')").fetchone() == (None,)


def test_multiple_statements_are_refused_as_such(api, store, data_agent):
    result = query(api, data_agent, "SELECT 1; SELECT 2")

    assert "multiple commands" in result["error"]


def test_writes_that_return_rows_are_refused_by_the_read_only_transaction(api, store, data_agent):
    result = query(api, data_agent, "UPDATE data.fish SET name = 'eel' RETURNING name")

    assert "read-only transaction" in result["error"]


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN DELETE FROM data.fish; END $$",
        "SET statement_timeout = 0",
        "COMMIT",
        "LISTEN fish",
        "PREPARE p AS SELECT 1",
        "COPY data.fish TO STDOUT",
    ],
)
def test_statements_that_return_no_rows_are_never_run(api, store, data_agent, sql):
    result = query(api, data_agent, sql)

    assert result == {"error": "Only queries that return rows can be run (SELECT, WITH, VALUES)."}


@pytest.fixture
def short_timeout(store) -> str:
    """The store's read-only role's statement timeout, down to 300ms."""
    with psycopg.connect(
        libpq_conninfo(get_settings().data_store_provisioner_url), autocommit=True
    ) as conn:
        conn.execute(f"ALTER ROLE \"{store.role_prefix}_ro\" SET statement_timeout = '300ms'")
    dispose_engines()  # the role's settings apply to new sessions
    return "SELECT pg_sleep(5)"


def stopped(result: dict) -> bool:
    return set(result) == {"error"} and "time limit" in result["error"]


def test_a_slow_query_hits_the_timeout(api, data_agent, short_timeout):
    started = time.monotonic()
    result = query(api, data_agent, short_timeout)

    assert stopped(result)
    assert time.monotonic() - started < 4


def test_a_query_cancelled_from_elsewhere_isnt_called_slow(api, store, data_agent):
    """Someone else cancelling it (an operator's `pg_cancel_backend`) isn't the time limit, so
    the model isn't told to try a cheaper query."""

    def cancel():
        with psycopg.connect(login_url(store, "ro"), autocommit=True) as conn:
            for _ in range(100):
                cancelled = conn.execute(
                    "SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
                    " WHERE query = 'SELECT pg_sleep(5)' AND pid <> pg_backend_pid()"
                ).fetchall()
                if cancelled:
                    return
                time.sleep(0.05)

    canceller = threading.Thread(target=cancel)
    canceller.start()
    result = query(api, data_agent, "SELECT pg_sleep(5)")
    canceller.join()

    assert set(result) == {"error"} and "user request" in result["error"]
    assert not stopped(result)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT set_config('statement_timeout', '0', false), pg_sleep(5)",
        "SELECT set_config('statement_timeout', '0', true), pg_sleep(5)",
        "WITH lifted AS MATERIALIZED (SELECT set_config('statement_timeout', '0', false))"
        " SELECT pg_sleep(5) FROM lifted",
    ],
)
def test_a_query_cant_lift_its_own_timeout(api, data_agent, short_timeout, sql):
    assert stopped(query(api, data_agent, sql))


@pytest.mark.parametrize(
    "lift",
    [
        "SET statement_timeout = 0",
        "SET SESSION statement_timeout = 0",
        "SELECT set_config('statement_timeout', '0', false)",
        "ALTER ROLE CURRENT_USER SET statement_timeout = 0",
        "COMMIT",
    ],
)
def test_nothing_a_query_sets_carries_over_to_the_next(api, data_agent, short_timeout, lift):
    """The pool keeps one connection per store and role here, so the next call gets the very
    session the first ran in."""
    lifted, slow = run_tools(
        api,
        data_agent,
        ToolCall("query_data", {"sql": lift}),
        ToolCall("query_data", {"sql": short_timeout}),
    )

    assert stopped(slow)


def test_results_over_the_row_cap_come_back_truncated(api, store, data_agent, monkeypatch):
    monkeypatch.setattr(get_settings(), "query_data_max_rows", 2)

    result = query(api, data_agent, "SELECT name FROM data.fish ORDER BY id")

    assert result["rows"] == [["pike"], ["perch"]]
    assert (result["row_count"], result["truncated"]) == (2, True)
    assert "first 2 rows" in result["note"]
    # Exactly at the cap is the whole result.
    result = query(api, data_agent, "SELECT name FROM data.fish ORDER BY id LIMIT 2")
    assert (result["row_count"], result["truncated"]) == (2, False)


def test_results_over_the_byte_cap_come_back_truncated(api, store, data_agent, monkeypatch):
    # Each row is `["pike"]` as JSON, and a separator: 9 bytes.
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 20)

    result = query(api, data_agent, "SELECT name FROM data.fish ORDER BY id")

    assert result["rows"] == [["pike"], ["perch"]]
    assert result["truncated"] is True


def test_a_first_row_over_the_byte_cap_is_said_to_be_too_big(api, store, data_agent, monkeypatch):
    """So the model knows to leave out or cut down a big column, not to narrow the query."""
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 100)

    result = query(api, data_agent, "SELECT 1 AS id, repeat('x', 200) AS body")

    assert (result["rows"], result["truncated"]) == ([], True)
    assert "first row alone" in result["note"]
    assert "first 0 rows" not in result["note"]


def test_a_huge_result_is_cut_short_without_reading_it_all(api, data_agent, short_timeout):
    """Rows are streamed, so a result far past the caps stops at them, well inside the
    timeout, rather than being read whole (or timing out)."""
    result = query(api, data_agent, "SELECT generate_series(1, 100000000)")

    assert result["row_count"] == get_settings().query_data_max_rows
    assert result["rows"][:2] == [[1], [2]]
    assert result["truncated"] is True


def test_a_value_over_the_byte_cap_is_never_loaded(api, store, data_agent):
    """One row is one value too: a value far past the byte cap stops the result without being
    turned into Python (and JSON), so it can't take the API's memory with it."""
    tracemalloc.start()
    try:
        result = query(api, data_agent, "SELECT 'small' UNION ALL SELECT repeat('x', 50000000)")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["rows"] == [["small"]]
    assert result["truncated"] is True
    # Loading the value would take 50 MB for the string alone, and as much again as JSON.
    assert peak < 20_000_000
    # Nor does it keep any (the connection's buffer is thrown away), and the next call works.
    assert query(api, data_agent, "SELECT 1")["rows"] == [[1]]


def test_values_come_back_as_json(api, store, data_agent):
    result = query(
        api,
        data_agent,
        "SELECT 1.10::numeric AS n, 'NaN'::float8 AS f, date '2026-10-16' AS d,"
        " '\\x0102'::bytea AS b, '{\"a\": [1]}'::jsonb AS j, ARRAY[1, 2] AS a,"
        " '[1,2]'::extensions.vector AS v, NULL AS z",
    )

    assert result["columns"] == ["n", "f", "d", "b", "j", "a", "v", "z"]
    assert result["rows"] == [
        ["1.10", "nan", "2026-10-16", "\\x0102", {"a": [1]}, [1, 2], "[1,2]", None]
    ]


def test_dates_times_and_intervals_come_back_as_postgres_writes_them(api, store, data_agent):
    """Including those Python can't hold (infinities, years past 9999 or BC, 24:00), so one
    such row doesn't fail the whole query."""
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.events (id int, at timestamptz, day date)")
        conn.execute(
            "INSERT INTO data.events VALUES (1, '2026-10-16 12:00+00', '2026-10-16'),"
            " (2, 'infinity', '-infinity')"
        )

    assert query(api, data_agent, "SELECT * FROM data.events ORDER BY id") == {
        "columns": ["id", "at", "day"],
        "rows": [[1, "2026-10-16 12:00:00+00", "2026-10-16"], [2, "infinity", "-infinity"]],
        "row_count": 2,
        "truncated": False,
    }

    result = query(
        api,
        data_agent,
        "SELECT '10000-01-01'::timestamp, '0044-03-15 BC'::date, '24:00'::time,"
        " interval '1 mon 2 days 03:00', interval '1000000000 days',"
        " ARRAY['infinity'::date], '[2026-01-01,infinity)'::daterange",
    )
    assert result["rows"] == [
        [
            "10000-01-01 00:00:00",
            "0044-03-15 BC",
            "24:00:00",
            "1 mon 2 days 03:00:00",
            "1000000000 days",
            ["infinity"],
            "[2026-01-01,infinity)",
        ]
    ]


@pytest.mark.parametrize(
    "sql",
    [
        # Nested deeper than Python's JSON decoder goes (Postgres goes much deeper).
        "SELECT (repeat('[', 5000) || repeat(']', 5000))::jsonb",
        # Text the model sent that isn't valid Unicode (a lone surrogate).
        "SELECT '\ud800'",
    ],
)
def test_a_query_that_fails_outside_postgres_comes_back_as_an_error(api, store, data_agent, sql):
    failed, after = run_tools(
        api,
        data_agent,
        ToolCall("query_data", {"sql": sql}),
        ToolCall("query_data", {"sql": "SELECT count(*) FROM data.fish"}),
    )

    assert set(failed) == {"error"}
    assert after["rows"] == [[3]]


def test_a_query_with_no_rows_still_names_its_columns(api, store, data_agent):
    result = query(api, data_agent, "SELECT name, weight_kg FROM data.fish WHERE false")

    assert result == {
        "columns": ["name", "weight_kg"],
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }


@pytest.fixture
def other_store(db, new_user) -> DataStore:
    """Another user's provisioned store, with a `data.secrets` table."""
    row = provisioned(db, data_store_for(new_user().id))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")
        conn.execute("INSERT INTO data.secrets VALUES ('hunter2')")
    return row


def test_neither_tool_reaches_another_store_or_the_platform(api, store, other_store, data_agent):
    platform = make_url(get_settings().database_url).database
    described = describe(api, data_agent)
    assert {(t["schema"], t["name"]) for t in described["tables"]} == {
        ("data", "fish"),
        ("aios", "schema_history"),
    }
    assert query(api, data_agent, "SELECT current_database()")["rows"] == [[store.dbname]]

    attempts = {
        # Cross-database references.
        f"SELECT * FROM {other_store.dbname}.data.secrets": "cross-database references",
        f'SELECT * FROM "{other_store.dbname}".data.secrets': "cross-database references",
        f"SELECT * FROM {platform}.public.users": "cross-database references",
        # Only the store's own schemas are there.
        "SELECT * FROM data.secrets": "does not exist",
        "SELECT * FROM users": "does not exist",
        "SELECT * FROM public.users": "does not exist",
        # psql's meta-commands aren't SQL.
        f"\\c {other_store.dbname}": "syntax error",
        f"\\connect {platform}": "syntax error",
        # dblink and postgres_fdw are on the server, but not in stores, and can't be added.
        f"SELECT * FROM dblink('dbname={other_store.dbname}', 'SELECT code FROM data.secrets')"
        " AS t(code text)": "does not exist",
        "SELECT * FROM extensions.dblink('dbname=postgres', 'SELECT 1') AS t(i int)": (
            "does not exist"
        ),
        "CREATE EXTENSION dblink": "Only queries that return rows",
        # Nor can it read files or run programs on the server.
        "SELECT pg_read_file('/etc/passwd')": "permission denied",
        "SELECT * FROM pg_ls_dir('.')": "permission denied",
        "SELECT lo_import('/etc/passwd')": "permission denied",
    }
    for sql, refusal in attempts.items():
        result = query(api, data_agent, sql)
        assert set(result) == {"error"}, sql
        assert refusal in result["error"], (sql, result)


def test_each_run_reads_its_own_users_store(api, store, other_store, data_agent):
    """The store comes from the run (its user), never from anything the model says."""
    with SessionLocal() as session:
        other_user = session.get(User, other_store.owner_user_id)
    app.dependency_overrides[current_user] = lambda: other_user
    their_agent = make_agent(api, "Theirs")
    their_agent = api.put(
        f"/api/agents/{their_agent['id']}", json=agent_body(their_agent, tools=DATA_TOOLS)
    ).json()

    described = describe(api, their_agent)
    result = query(api, their_agent, "SELECT code FROM data.secrets")

    assert {(t["schema"], t["name"]) for t in described["tables"]} == {
        ("data", "secrets"),
        ("aios", "schema_history"),
    }
    assert result["rows"] == [["hunter2"]]
    assert "does not exist" in query(api, their_agent, "SELECT * FROM data.fish")["error"]


@pytest.mark.parametrize(
    ("status", "says"),
    [
        (DataStoreStatus.provisioning, "still being set up"),
        # Not "still being set up": the user would be told to wait for something that may never
        # come, while the app says it failed and will be retried.
        (DataStoreStatus.failed, "couldn't be set up"),
        (DataStoreStatus.deleting, "isn't available"),
        (DataStoreStatus.deleted, "isn't available"),
    ],
)
def test_both_tools_say_why_the_store_isnt_ready(api, db, user, data_agent, status, says):
    db.execute(update(DataStore).where(DataStore.owner_user_id == user.id).values(status=status))
    db.commit()

    described, queried = run_tools(
        api,
        data_agent,
        ToolCall("describe_data", {}),
        ToolCall("query_data", {"sql": "SELECT 1"}),
    )

    assert described == queried
    assert says in described["error"]


def test_the_tools_keep_working_call_after_call(api, store, data_agent):
    """Psycopg prepares statements it runs often, which resetting the session drops."""
    calls = [ToolCall("query_data", {"sql": "SELECT count(*) FROM data.fish"})] * 8
    calls += [ToolCall("describe_data", {})] * 8 + calls

    outputs = run_tools(api, data_agent, *calls)

    assert [o.get("error") for o in outputs] == [None] * len(calls)


def test_read_only_connections_prepare_nothing_whether_or_not_they_ran_a_query(
    api, store, data_agent
):
    """`query_data` has psycopg prepare nothing on its connection (its reset would drop them),
    so no other user of the read-only engine does either, whatever the pooled connection last
    served."""

    def prepared_by_a_busy_session() -> tuple[int, list]:
        with store_session(store.id, "ro") as session:
            for _ in range(8):  # past psycopg's default threshold (5)
                session.execute(text("SELECT count(*) FROM data.fish"))
            pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            return pid, session.execute(text("SELECT name FROM pg_prepared_statements")).all()

    pid, before = prepared_by_a_busy_session()
    assert "error" not in query(api, data_agent, "SELECT 1")
    same_pid, after = prepared_by_a_busy_session()

    assert same_pid == pid  # the same pooled connection, before and after the query
    assert before == after == []


def test_a_query_cant_hold_a_lock_past_its_call(api, store, data_agent):
    run_tools(api, data_agent, ToolCall("query_data", {"sql": "SELECT pg_advisory_lock(42)"}))

    with psycopg.connect(login_url(store, "ro")) as conn:
        assert conn.execute("SELECT pg_try_advisory_lock(42)").fetchone() == (True,)


def test_slow_queries_dont_starve_the_apis_other_requests(api, store, data_agent, monkeypatch):
    """A query holds a worker thread while it runs, or while it waits for one of the store's
    few connections, and the API's sync endpoints run on the same threads. So a model asking
    for many slow queries at once mustn't take them all. (The thread pool is shrunk from its 40
    to keep the test quick.)"""
    monkeypatch.setattr(get_settings(), "scheduler_enabled", False)
    sql = "SELECT pg_sleep(0.5)"
    threads = 16

    async def script(messages, info):
        if last_tool_results(messages):
            yield "done"
        else:
            for _ in range(threads + 4):
                yield ToolCall("query_data", {"sql": sql})

    def sleeping() -> int:
        with psycopg.connect(login_url(store, "ro")) as conn:
            query = "SELECT count(*) FROM pg_stat_activity WHERE query = %s"
            return conn.execute(query, (sql,)).fetchone()[0]

    def shrink() -> None:
        to_thread.current_default_thread_limiter().total_tokens = threads

    with api, use_model(model(script)):
        api.portal.call(shrink)
        run = api.post(f"/api/agents/{data_agent['id']}/runs").json()
        url = f"/api/agents/{data_agent['id']}/runs/{run['id']}/chat"
        with ThreadPoolExecutor(1) as pool:
            turn = pool.submit(api.post, url, json=chat_body("go"))
            while not sleeping():
                time.sleep(0.01)
            start = time.monotonic()
            assert api.get("/api/tools").status_code == 200
            waited = time.monotonic() - start
            assert turn.result().status_code == 200

    assert waited < 0.25
