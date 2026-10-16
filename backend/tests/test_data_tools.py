"""The agent tools on the data store: `describe_data` and `query_data` (`aios.tools.data`).

These run against real, provisioned stores. Nothing can change a store's `data` schema yet
(schema change requests are to come), so the tests make `data` tables directly, as the store's
owner role.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from fake_model import ToolCall, last_tool_results, model, tool_output, use_model
from sqlalchemy import update
from sqlalchemy.engine import make_url
from test_agents import agent_body, chat_body, make_agent
from test_provisioning import login_url, new_user, store_row  # noqa: F401  (new_user: fixture)

from aios.auth import current_user
from aios.config import get_settings, libpq_conninfo
from aios.datastores import data_store_for, provision
from aios.datastores.connections import dispose_engines
from aios.db import SessionLocal
from aios.main import app
from aios.models import DataStore, DataStoreStatus, User
from aios.tools import build_tool
from aios.tools.data import STORE_NOT_READY

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


def test_a_huge_result_is_cut_short_without_reading_it_all(api, data_agent, short_timeout):
    """Rows are streamed, so a result far past the caps stops at them, well inside the
    timeout, rather than being read whole (or timing out)."""
    result = query(api, data_agent, "SELECT generate_series(1, 100000000)")

    assert result["row_count"] == get_settings().query_data_max_rows
    assert result["rows"][:2] == [[1], [2]]
    assert result["truncated"] is True


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


def test_a_query_with_no_rows_still_names_its_columns(api, store, data_agent):
    result = query(api, data_agent, "SELECT name, weight_kg FROM data.fish WHERE false")

    assert result == {
        "columns": ["name", "weight_kg"],
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }


@pytest.fixture
def other_store(db, new_user) -> DataStore:  # noqa: F811  (new_user is the fixture)
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


@pytest.mark.parametrize("status", [DataStoreStatus.provisioning, DataStoreStatus.failed])
def test_both_tools_refuse_while_the_store_isnt_ready(api, db, user, data_agent, status):
    db.execute(update(DataStore).where(DataStore.owner_user_id == user.id).values(status=status))
    db.commit()

    described, queried = run_tools(
        api,
        data_agent,
        ToolCall("describe_data", {}),
        ToolCall("query_data", {"sql": "SELECT 1"}),
    )

    assert described == queried == {"error": STORE_NOT_READY}
    assert "still being set up" in STORE_NOT_READY


def test_the_tools_keep_working_call_after_call(api, store, data_agent):
    """Psycopg prepares statements it runs often, which resetting the session drops."""
    calls = [ToolCall("query_data", {"sql": "SELECT count(*) FROM data.fish"})] * 8
    calls += [ToolCall("describe_data", {})] * 8 + calls

    outputs = run_tools(api, data_agent, *calls)

    assert [o.get("error") for o in outputs] == [None] * len(calls)


def test_a_query_cant_hold_a_lock_past_its_call(api, store, data_agent):
    run_tools(api, data_agent, ToolCall("query_data", {"sql": "SELECT pg_advisory_lock(42)"}))

    with psycopg.connect(login_url(store, "ro")) as conn:
        assert conn.execute("SELECT pg_try_advisory_lock(42)").fetchone() == (True,)
