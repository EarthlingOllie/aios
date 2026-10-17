"""The agent tool that writes to the data store: `write_data` (`aios.tools.write_data`).

Like `test_data_tools`, these run against real, provisioned stores, and make `data` tables
directly as the store's owner role (standing in for approved schema changes).
"""

import psycopg
import pytest
from fake_model import ToolCall
from sqlalchemy import update
from test_agents import agent_body, make_agent
from test_data_tools import as_owner, provisioned, run_tools

from aios.config import get_settings, libpq_conninfo
from aios.datastores import data_store_for, store_not_ready
from aios.datastores.connections import dispose_engines
from aios.models import DataStore, DataStoreStatus
from aios.tools import build_tool

TOOLS = [
    {"key": "write_data", "config": {}},
    {"key": "query_data", "config": {}},
    {"key": "describe_data", "config": {}},
]


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in user's store, with a hand-made `data.fish` table holding two fish."""
    row = provisioned(db, data_store_for(user.id))
    with as_owner(row) as conn:
        conn.execute(
            "CREATE TABLE data.fish (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " name text NOT NULL UNIQUE, weight_kg numeric, caught_on date)"
        )
        conn.execute("INSERT INTO data.fish (name, weight_kg) VALUES ('pike', 4.2), ('perch', 0.8)")
    return row


@pytest.fixture
def agent(api) -> dict:
    agent = make_agent(api, "Writer")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def write(api, agent, **args) -> dict:
    return run_tools(api, agent, ToolCall("write_data", args))[0]


def call(**args) -> ToolCall:
    return ToolCall("write_data", args)


def fish(store: DataStore) -> list[tuple]:
    with as_owner(store) as conn:
        return conn.execute(
            "SELECT name, weight_kg::text, caught_on::text FROM data.fish ORDER BY id"
        ).fetchall()


def test_the_tool_is_in_the_library_and_says_what_it_takes(api, monkeypatch):
    tools = {t["key"]: t for t in api.get("/api/tools").json()}
    assert tools["write_data"]["default_config"] == {}

    monkeypatch.setattr(get_settings(), "write_data_max_rows", 7)
    [tool] = build_tool("write_data", {})
    assert "stored as data" in tool.description
    assert "not instructions" in tool.description
    assert "At most 7 rows" in tool.description
    assert set(tool.args) == {"table", "operation", "rows", "values", "where", "on_conflict"}


def test_an_agent_inserts_rows(api, store, agent):
    result = write(
        api,
        agent,
        table="fish",
        operation="insert",
        rows=[{"name": "carp", "weight_kg": 7.5, "caught_on": "2026-10-16"}, {"name": "eel"}],
    )

    assert result == {
        "table": "data.fish",
        "operation": "insert",
        "row_count": 2,
        "rows": [{"id": 3}, {"id": 4}],
    }
    assert fish(store) == [
        ("pike", "4.2", None),
        ("perch", "0.8", None),
        ("carp", "7.5", "2026-10-16"),
        ("eel", None, None),
    ]


def test_an_agent_upserts_rows_by_a_unique_column(api, store, agent):
    result = write(
        api,
        agent,
        table="data.fish",
        operation="upsert",
        rows=[{"name": "pike", "weight_kg": "5.10"}, {"name": "carp", "weight_kg": 7}],
        on_conflict=["name"],
    )

    assert (result["row_count"], result["rows"]) == (2, [{"id": 1}, {"id": 4}])
    assert fish(store) == [("pike", "5.10", None), ("perch", "0.8", None), ("carp", "7", None)]


def test_an_upsert_matches_on_the_primary_key_by_default(api, store, agent):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.tags (code text PRIMARY KEY, label text)")
        conn.execute("INSERT INTO data.tags VALUES ('a', 'Alpha')")

    result = write(
        api,
        agent,
        table="tags",
        operation="upsert",
        rows=[{"code": "a", "label": "Ay"}, {"code": "b", "label": "Bee"}],
    )

    assert result["rows"] == [{"code": "a"}, {"code": "b"}]
    with as_owner(store) as conn:
        assert conn.execute("SELECT * FROM data.tags ORDER BY code").fetchall() == [
            ("a", "Ay"),
            ("b", "Bee"),
        ]


def test_an_upsert_cant_match_on_a_key_the_database_sets(api, store, agent):
    result = write(api, agent, table="fish", operation="upsert", rows=[{"name": "pike"}])

    assert "lacks id" in result["error"]
    assert "on_conflict" in result["error"]


def test_an_upsert_needs_a_unique_constraint_to_match_on(api, store, agent):
    result = write(
        api,
        agent,
        table="fish",
        operation="upsert",
        rows=[{"name": "pike", "weight_kg": 1}],
        on_conflict=["weight_kg"],
    )

    assert "no unique or exclusion constraint" in result["error"]


def test_an_agent_updates_rows_by_primary_key_or_other_columns(api, store, agent):
    by_key, by_name = run_tools(
        api,
        agent,
        call(table="fish", operation="update", values={"weight_kg": 1}, where={"id": 2}),
        call(
            table="fish",
            operation="update",
            values={"caught_on": "2026-10-01"},
            where={"name": "pike", "caught_on": None},
        ),
    )

    assert (by_key["row_count"], by_key["rows"]) == (1, [{"id": 2}])
    assert (by_name["row_count"], by_name["rows"]) == (1, [{"id": 1}])
    assert fish(store) == [("pike", "4.2", "2026-10-01"), ("perch", "1", None)]


def test_an_agent_deletes_rows(api, store, agent):
    result = write(api, agent, table="fish", operation="delete", where={"name": "perch"})

    assert (result["row_count"], result["rows"]) == (1, [{"id": 2}])
    assert fish(store) == [("pike", "4.2", None)]


def test_nothing_matching_changes_nothing(api, store, agent):
    result = write(api, agent, table="fish", operation="delete", where={"id": 99})

    assert (result["row_count"], result["rows"]) == (0, [])
    assert len(fish(store)) == 2


def test_a_table_with_no_primary_key_says_only_how_many_rows(api, store, agent):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.notes (body text)")

    result = write(api, agent, table="notes", operation="insert", rows=[{"body": "hi"}])

    assert result == {"table": "data.notes", "operation": "insert", "row_count": 1}


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "update", "values": {"name": "eel"}},
        {"operation": "update", "values": {"name": "eel"}, "where": {}},
        {"operation": "delete"},
        {"operation": "delete", "where": {}},
    ],
)
def test_unfiltered_updates_and_deletes_are_refused(api, store, agent, args):
    result = write(api, agent, table="fish", **args)

    assert set(result) == {"error"}
    assert "Name the rows to change with `where`" in result["error"]
    assert "its primary key, id" in result["error"]
    assert "Every row at once isn't allowed" in result["error"]
    assert len(fish(store)) == 2


@pytest.mark.parametrize(
    ("args", "says"),
    [
        ({"table": "eels", "operation": "insert", "rows": [{"name": "x"}]}, "no table `eels`"),
        ({"table": "eels", "operation": "insert", "rows": [{"name": "x"}]}, "its tables: fish"),
        (
            {"table": "fish", "operation": "insert", "rows": [{"colour": "x"}]},
            "rows[0]: `data.fish` has no column `colour`. Its columns: id, name, weight_kg,",
        ),
        (
            {"table": "fish", "operation": "update", "values": {"name": "x"}, "where": {"c": 1}},
            "where: `data.fish` has no column `c`",
        ),
        (
            {"table": "fish", "operation": "insert", "rows": [{"name": "a"}, {"name": 5}]},
            "rows[1]: `name` is text, so it takes a string; got int 5",
        ),
        (
            {"table": "fish", "operation": "insert", "rows": [{"name": "a", "weight_kg": "heavy"}]},
            "`weight_kg` is numeric, so it takes a number",
        ),
        (
            {"table": "fish", "operation": "insert", "rows": [{"name": "a", "weight_kg": True}]},
            "takes a number",
        ),
        (
            {"table": "fish", "operation": "delete", "where": {"id": "one"}},
            "`id` is bigint, so it takes a whole number",
        ),
        ({"table": "fish", "operation": "delete", "where": {"id": 2**63}}, "a whole number from"),
        (
            {"table": "fish", "operation": "insert", "rows": [{"name": "a", "caught_on": "soon"}]},
            'rows[0]: invalid input syntax for type date: "soon"',
        ),
        (
            {"table": "fish", "operation": "insert", "rows": [{"id": 9, "name": "a"}]},
            "`id` is set by the database",
        ),
        ({"table": "fish", "operation": "insert", "rows": [{"name": "pike"}]}, "already exists"),
        (
            {"table": "fish", "operation": "insert", "rows": [{"weight_kg": 1}]},
            'null value in column "name"',
        ),
        ({"table": "fish", "operation": "insert", "rows": []}, "needs `rows`"),
        (
            {"table": "fish", "operation": "insert", "rows": [{"name": "a"}], "where": {"id": 1}},
            "takes `rows`, not `values` or `where`",
        ),
        (
            {"table": "fish", "operation": "update", "where": {"id": 1}},
            "`values` must be an object",
        ),
    ],
)
def test_bad_calls_are_refused_with_what_to_fix(api, store, agent, args, says):
    result = write(api, agent, **args)

    assert set(result) == {"error"}
    assert says in result["error"], result
    assert len(fish(store)) == 2


def test_a_call_is_written_whole_or_not_at_all(api, store, agent):
    result = write(
        api, agent, table="fish", operation="insert", rows=[{"name": "eel"}, {"name": "pike"}]
    )

    assert result["error"].startswith("rows[1]: ")
    assert len(fish(store)) == 2


@pytest.mark.parametrize(
    "table", ["aios.schema_history", "aios.embedding_columns", "public.fish", "schema_history"]
)
def test_only_data_tables_can_be_written(api, store, agent, table):
    with as_owner(store) as conn:
        conn.execute(
            "INSERT INTO aios.schema_history (id, summary, sql) OVERRIDING SYSTEM VALUE"
            " VALUES (1, 'x', 'x')"
        )
        conn.execute("INSERT INTO aios.embedding_columns VALUES ('fish', 'id', 'name')")

    result = write(api, agent, table=table, operation="delete", where={"id": 1})

    assert set(result) == {"error"}
    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM aios.schema_history").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM aios.embedding_columns").fetchone() == (1,)


def test_an_aios_table_is_refused_as_the_platforms(api, store, agent):
    result = write(api, agent, table="aios.schema_history", operation="delete", where={"id": 1})

    assert "Only tables in the `data` schema" in result["error"]


def test_inserts_over_the_row_cap_are_refused(api, store, agent, monkeypatch):
    monkeypatch.setattr(get_settings(), "write_data_max_rows", 2)

    over, at = run_tools(
        api,
        agent,
        call(table="fish", operation="insert", rows=[{"name": n} for n in "abc"]),
        call(table="fish", operation="insert", rows=[{"name": n} for n in "de"]),
    )

    assert "That's 3 rows; one call writes at most 2" in over["error"]
    assert at["row_count"] == 2
    assert len(fish(store)) == 4


def test_updates_and_deletes_over_the_row_cap_are_refused_and_change_nothing(
    api, store, agent, monkeypatch
):
    monkeypatch.setattr(get_settings(), "write_data_max_rows", 1)

    updated, deleted = run_tools(
        api,
        agent,
        call(table="fish", operation="update", values={"weight_kg": 0}, where={"caught_on": None}),
        call(table="fish", operation="delete", where={"caught_on": None}),
    )

    assert "would touch 2 rows; one call changes at most 1" in updated["error"]
    assert "Nothing was changed" in updated["error"]
    assert "would touch 2 rows" in deleted["error"]
    assert fish(store) == [("pike", "4.2", None), ("perch", "0.8", None)]


@pytest.mark.parametrize(
    "status", [DataStoreStatus.provisioning, DataStoreStatus.failed, DataStoreStatus.deleting]
)
def test_it_says_why_the_store_isnt_ready(api, db, user, agent, status):
    db.execute(update(DataStore).where(DataStore.owner_user_id == user.id).values(status=status))
    db.commit()

    result = write(api, agent, table="fish", operation="delete", where={"id": 1})

    assert result == {"error": store_not_ready(status)}


def test_a_store_that_cant_be_reached_is_an_error_for_the_model(api, store, agent):
    url = get_settings().data_store_provisioner_url
    with psycopg.connect(libpq_conninfo(url), autocommit=True) as provisioner:
        provisioner.execute(f'ALTER ROLE "{store.role_prefix}_rw" CONNECTION LIMIT 0')
    dispose_engines()

    result = write(api, agent, table="fish", operation="delete", where={"id": 1})

    assert set(result) == {"error"}
    assert "couldn't be reached" in result["error"]
    assert store.role_prefix not in result["error"]


def test_it_writes_as_the_stores_read_write_role(api, store, agent):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.audit (id int PRIMARY KEY, who text DEFAULT current_user)")

    write(api, agent, table="audit", operation="insert", rows=[{"id": 1}])

    with as_owner(store) as conn:
        assert conn.execute("SELECT who FROM data.audit").fetchone() == (f"{store.role_prefix}_rw",)


def test_values_of_each_type(api, store, agent):
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.things (id int PRIMARY KEY, flag boolean, meta jsonb, ref uuid,"
            " at timestamptz, labels text[], score float8, v extensions.vector(3))"
        )

    result = write(
        api,
        agent,
        table="things",
        operation="insert",
        rows=[
            {
                "id": 1,
                "flag": True,
                "meta": {"a": [1, "two"]},
                "ref": "6f9619ff-8b86-d011-b42d-00c04fc964ff",
                "at": "2026-10-16T12:00:00Z",
                "labels": ["x", "y"],
                "score": 0.5,
                "v": [1, 2, 3],
            }
        ],
    )

    assert result["row_count"] == 1, result
    with as_owner(store) as conn:
        row = conn.execute(
            "SELECT flag, meta, ref::text, at = '2026-10-16 12:00+00', labels, score, v::text"
            " FROM data.things"
        ).fetchone()
    assert row == (
        True,
        {"a": [1, "two"]},
        "6f9619ff-8b86-d011-b42d-00c04fc964ff",
        True,
        ["x", "y"],
        0.5,
        "[1,2,3]",
    )


def test_text_that_looks_like_sql_is_stored_as_it_is(api, store, agent):
    name = "x'); DROP TABLE data.fish; --"

    write(api, agent, table="fish", operation="insert", rows=[{"name": name}])

    assert fish(store)[-1] == (name, None, None)


def test_names_the_catalog_has_are_quoted(api, store, agent):
    with as_owner(store) as conn:
        conn.execute('CREATE TABLE data."Odd Table" ("Mixed Case" text, "select" int)')

    result = write(
        api, agent, table="Odd Table", operation="insert", rows=[{"Mixed Case": "a", "select": 1}]
    )

    assert result["row_count"] == 1, result
