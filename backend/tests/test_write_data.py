"""The agent tool that writes to the data store: `write_data` (`aios.tools.write_data`).

Like `test_data_tools`, these run against real, provisioned stores, and make `data` tables
directly as the store's owner role (standing in for approved schema changes).
"""

import math

import psycopg
import pytest
from conftest import add_document
from fake_model import ToolCall
from sqlalchemy import update
from test_agents import agent_body, make_agent
from test_data_tools import as_owner, provisioned, run_tools
from test_provisioning import login_url

from aios import worker
from aios.config import get_settings, libpq_conninfo
from aios.datastores import data_store_for, store_not_ready
from aios.datastores.connections import dispose_engines
from aios.models import DataStore, DataStoreStatus
from aios.tools import build_tool, write_data

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


def test_a_constraint_checked_at_commit_is_refused_with_what_failed(api, store, agent):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.ponds (id int PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE data.stocked (id int PRIMARY KEY,"
            " pond int REFERENCES data.ponds DEFERRABLE INITIALLY DEFERRED)"
        )

    result = write(api, agent, table="stocked", operation="insert", rows=[{"id": 1, "pond": 9}])

    assert set(result) == {"error"}
    assert "violates foreign key constraint" in result["error"], result
    assert "Key (pond)=(9) is not present" in result["error"]


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

    assert "matches more than 1 row; one call changes at most 1" in updated["error"]
    assert "Nothing was changed" in updated["error"]
    assert "matches more than 1 row" in deleted["error"]
    assert fish(store) == [("pike", "4.2", None), ("perch", "0.8", None)]


def test_updates_and_deletes_over_the_row_cap_are_refused_before_they_run(
    api, store, agent, monkeypatch
):
    """Not run and rolled back: an unfiltered-enough one would lock (and return) every row."""
    monkeypatch.setattr(get_settings(), "write_data_max_rows", 1)
    with as_owner(store) as conn:
        conn.execute(
            "CREATE FUNCTION data.ran() RETURNS trigger LANGUAGE plpgsql AS"
            " $$ BEGIN RAISE EXCEPTION 'the statement ran'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER ran BEFORE UPDATE OR DELETE ON data.fish"
            " FOR EACH ROW EXECUTE FUNCTION data.ran()"
        )

    updated, deleted = run_tools(
        api,
        agent,
        call(table="fish", operation="update", values={"weight_kg": 0}, where={"caught_on": None}),
        call(table="fish", operation="delete", where={"caught_on": None}),
    )

    for result in (updated, deleted):
        assert "one call changes at most 1" in result["error"]
        assert "the statement ran" not in result["error"]


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


def test_jsonb_can_be_filtered_on_but_json_cant(api, store, agent):
    """Postgres has no equality for json; jsonb's is by value."""
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.docs (id int PRIMARY KEY, meta json, tags jsonb)")
        conn.execute("""INSERT INTO data.docs VALUES (1, '{"a": 1}', '{"b": 2}')""")

    by_json, by_jsonb = run_tools(
        api,
        agent,
        call(table="docs", operation="delete", where={"meta": {"a": 1}}),
        call(table="docs", operation="delete", where={"tags": {"b": 2}}),
    )

    assert by_json == {
        "error": "where: `meta` is json, which can't be compared, so it can't be filtered on."
    }
    assert by_jsonb["row_count"] == 1, by_jsonb


def test_a_vector_with_an_infinite_number_is_refused(api, store, agent):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.points (id int PRIMARY KEY, at extensions.vector(2))")

    result = write(
        api, agent, table="points", operation="insert", rows=[{"id": 1, "at": [math.inf, 1]}]
    )

    assert set(result) == {"error"}, result
    assert "rows[0]: infinite value not allowed in vector" in result["error"]


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


def test_names_with_percent_signs_are_written_like_any_other(api, store, agent):
    """Psycopg reads `%` as the start of a placeholder, even inside a quoted name."""
    with as_owner(store) as conn:
        conn.execute('CREATE TABLE data."100%" (id int PRIMARY KEY, "pct%" int, "a%sb" text)')

    inserted, updated = run_tools(
        api,
        agent,
        call(table="100%", operation="insert", rows=[{"id": 1, "pct%": 5, "a%sb": "x"}]),
        call(table="100%", operation="update", values={"pct%": 6}, where={"a%sb": "x"}),
    )

    assert (inserted.get("row_count"), updated.get("row_count")) == (1, 1), (inserted, updated)
    with as_owner(store) as conn:
        assert conn.execute('SELECT "pct%" FROM data."100%"').fetchall() == [(6,)]


# Embedding columns.


def fake_vector(text: str) -> list[float]:
    """A stand-in for the embedding model's: the text's length, then zeros."""
    return [float(len(text))] + [0.0] * 1023


@pytest.fixture
def embedded(monkeypatch) -> list[list[str]]:
    """Fakes the embedding model; records each batch of texts it's asked to embed."""
    batches: list[list[str]] = []

    async def embed_documents(texts):
        batches.append(list(texts))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(write_data, "embed_documents", embed_documents)
    return batches


@pytest.fixture
def notes(store) -> DataStore:
    """`data.notes`, whose `body_embedding` is marked as the embedding of `body`."""
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.notes (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " title text UNIQUE, body text, body_embedding extensions.vector(1024))"
        )
        conn.execute(
            "INSERT INTO aios.embedding_columns VALUES ('notes', 'body_embedding', 'body')"
        )
    return store


def stored_notes(store: DataStore) -> list[tuple]:
    """Each note's title, body, and its embedding's first value (the body's length, faked)."""
    with as_owner(store) as conn:
        return conn.execute(
            "SELECT title, body, (body_embedding::real[])[1] FROM data.notes ORDER BY id"
        ).fetchall()


def test_inserting_text_stores_its_embedding(api, notes, agent, embedded):
    inserted, queried = run_tools(
        api,
        agent,
        call(
            table="notes",
            operation="insert",
            rows=[{"title": "a", "body": "hello"}, {"title": "b", "body": "hi"}, {"title": "c"}],
        ),
        ToolCall(
            "query_data",
            {"sql": "SELECT title, vector_dims(body_embedding) FROM data.notes ORDER BY id"},
        ),
    )

    assert inserted["row_count"] == 3
    assert embedded == [["hello", "hi"]]  # one batch, for the call
    assert stored_notes(notes) == [("a", "hello", 5.0), ("b", "hi", 2.0), ("c", None, None)]
    assert queried["rows"] == [["a", 1024], ["b", 1024], ["c", None]]


def test_updating_the_text_re_embeds_it(api, notes, agent, embedded):
    run_tools(
        api,
        agent,
        call(table="notes", operation="insert", rows=[{"title": "a", "body": "hello"}]),
        call(table="notes", operation="update", values={"body": "hello there"}, where={"id": 1}),
    )
    assert stored_notes(notes) == [("a", "hello there", 11.0)]

    # Changing anything else leaves it be, and embeds nothing.
    write(api, agent, table="notes", operation="update", values={"title": "b"}, where={"id": 1})
    assert stored_notes(notes) == [("b", "hello there", 11.0)]
    assert embedded == [["hello"], ["hello there"]]

    # Nor does blank text have an embedding.
    write(api, agent, table="notes", operation="update", values={"body": " "}, where={"id": 1})
    assert stored_notes(notes) == [("b", " ", None)]


def test_upserting_the_text_re_embeds_it(api, notes, agent, embedded):
    run_tools(
        api,
        agent,
        call(table="notes", operation="insert", rows=[{"title": "a", "body": "hello"}]),
        call(
            table="notes",
            operation="upsert",
            rows=[{"title": "a", "body": "bye"}],
            on_conflict=["title"],
        ),
    )

    assert stored_notes(notes) == [("a", "bye", 3.0)]


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "insert", "rows": [{"title": "a", "body_embedding": [0.1] * 1024}]},
        {"operation": "update", "values": {"body_embedding": [0.1] * 1024}, "where": {"id": 1}},
    ],
)
def test_embeddings_are_never_sent(api, notes, agent, embedded, args):
    result = write(api, agent, table="notes", **args)

    assert "`body_embedding` is the embedding of `body`" in result["error"]
    assert "Leave it out" in result["error"]


def test_a_failing_embedding_model_writes_nothing(api, notes, agent, monkeypatch):
    async def embed_documents(texts):
        raise RuntimeError("provider down")

    monkeypatch.setattr(write_data, "embed_documents", embed_documents)

    result = write(api, agent, table="notes", operation="insert", rows=[{"body": "hello"}])

    assert set(result) == {"error"}
    assert "couldn't be embedded" in result["error"]
    assert "nothing was written" in result["error"]
    # It may never embed (text past the model's limit, say): not only "try again".
    assert "shorter" in result["error"]
    assert stored_notes(notes) == []


def test_embeddings_cant_be_filtered_on(api, notes, agent, embedded):
    result = write(
        api, agent, table="notes", operation="delete", where={"body_embedding": [0.1] * 1024}
    )

    assert "`body_embedding` is an embedding, and can't be filtered on" in result["error"]


def test_a_table_that_changes_while_its_text_is_embedded_is_refused(api, notes, agent, monkeypatch):
    """The embedding is made with no connection held, so the table is read again to write."""

    async def embed_documents(texts):
        with as_owner(notes) as conn:
            conn.execute(
                "ALTER TABLE data.notes ADD COLUMN title_embedding extensions.vector(1024)"
            )
            conn.execute(
                "INSERT INTO aios.embedding_columns VALUES ('notes', 'title_embedding', 'title')"
            )
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(write_data, "embed_documents", embed_documents)

    result = write(
        api, agent, table="notes", operation="insert", rows=[{"title": "a", "body": "hello"}]
    )

    assert result == {
        "error": "The table changed while writing, and nothing was written. Try again."
    }
    assert stored_notes(notes) == []


def test_describe_data_says_which_columns_are_embeddings(api, notes, agent):
    [described] = run_tools(api, agent, ToolCall("describe_data", {}))

    table = next(t for t in described["tables"] if t["name"] == "notes")
    columns = {c["name"]: c for c in table["columns"]}
    assert columns["body_embedding"]["embedding_of"] == "body"
    assert columns["body_embedding"]["type"] == "vector(1024)"
    assert "embedding_of" not in columns["body"]


def test_a_marker_whose_text_column_is_gone_refuses_writes(api, notes, agent, embedded):
    with as_owner(notes) as conn:
        conn.execute("ALTER TABLE data.notes DROP COLUMN body")

    result = write(api, agent, table="notes", operation="insert", rows=[{"title": "a"}])

    assert "needs a schema change" in result["error"]


def test_a_marked_column_of_the_wrong_size_refuses_writes_before_embedding(
    api, store, agent, embedded
):
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.tiny (id int PRIMARY KEY, body text, e extensions.vector(3))"
        )
        conn.execute("INSERT INTO aios.embedding_columns VALUES ('tiny', 'e', 'body')")

    result = write(api, agent, table="tiny", operation="insert", rows=[{"id": 1, "body": "hi"}])

    assert "`data.tiny.e` is marked as an embedding but isn't a vector(1024)" in result["error"]
    assert "needs a schema change" in result["error"]
    assert embedded == []


def test_each_distinct_text_is_embedded_once(api, store, agent, embedded):
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.pairs (id int PRIMARY KEY, body text,"
            " a extensions.vector(1024), b extensions.vector(1024))"
        )
        conn.execute("INSERT INTO aios.embedding_columns VALUES ('pairs', 'a', 'body')")
        conn.execute("INSERT INTO aios.embedding_columns VALUES ('pairs', 'b', 'body')")

    result = write(
        api,
        agent,
        table="pairs",
        operation="insert",
        rows=[{"id": 1, "body": "hello"}, {"id": 2, "body": "hello"}, {"id": 3, "body": "hi"}],
    )

    assert result["row_count"] == 3, result
    assert embedded == [["hello", "hi"]]
    with as_owner(store) as conn:
        assert conn.execute(
            "SELECT id, (a::real[])[1], (b::real[])[1] FROM data.pairs ORDER BY id"
        ).fetchall() == [(1, 5.0, 5.0), (2, 5.0, 5.0), (3, 2.0, 2.0)]


def test_a_store_not_yet_migrated_to_have_markers_is_described_and_written(api, store, agent):
    """A store whose migration failed stays ready at the revision before, under the new code."""
    with as_owner(store) as conn:
        conn.execute("DROP TABLE aios.embedding_columns")

    described, inserted = run_tools(
        api,
        agent,
        ToolCall("describe_data", {}),
        call(table="fish", operation="insert", rows=[{"name": "eel"}]),
    )

    assert "error" not in described, described
    assert ("data", "fish") in {(t["schema"], t["name"]) for t in described["tables"]}
    assert inserted.get("row_count") == 1, inserted


def test_only_the_owner_writes_the_markers(store):
    """The read-write role reads them, but can't mark or unmark columns (only schema changes,
    as the owner, do)."""
    with psycopg.connect(login_url(store, "rw"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM aios.embedding_columns").fetchone() == (0,)
        for statement in (
            "INSERT INTO aios.embedding_columns VALUES ('fish', 'name', 'name')",
            "DELETE FROM aios.embedding_columns",
            "TRUNCATE aios.embedding_columns",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
    with psycopg.connect(login_url(store, "ro"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM aios.embedding_columns").fetchone() == (0,)


def test_the_tool_reads_the_catalog_as_the_store_sees_it_now(api, store, agent):
    """No cache of a table: a column added since the last call is there at the next."""
    write(api, agent, table="fish", operation="delete", where={"id": 1})
    with as_owner(store) as conn:
        conn.execute("ALTER TABLE data.fish ADD COLUMN colour text")

    result = write(
        api, agent, table="fish", operation="insert", rows=[{"name": "eel", "colour": "grey"}]
    )

    assert result["row_count"] == 1


def test_arguments_of_the_wrong_shape_are_refused_by_the_tools_schema(api, store, agent):
    result = write(api, agent, table="fish", operation="insert", rows=["pike"])

    assert "valid dictionary" in result
    assert len(fish(store)) == 2


def test_a_store_at_its_database_cap_refuses_inserts_and_updates_but_deletes_and_reads(
    api, store, agent, monkeypatch
):
    """Checked as the call comes in, not against the last measurement."""
    monkeypatch.setattr(get_settings(), "data_store_db_quota_bytes", 1024)
    with as_owner(store) as conn:
        # 10 KB of notes.
        conn.execute("CREATE TABLE data.notes (body text)")
        conn.execute(
            "INSERT INTO data.notes SELECT repeat(md5(g::text), 32) FROM generate_series(1, 10) g"
        )

    inserted, upserted, updated, deleted, queried = run_tools(
        api,
        agent,
        call(table="fish", operation="insert", rows=[{"name": "carp"}]),
        call(table="fish", operation="upsert", rows=[{"name": "pike"}], on_conflict=["name"]),
        call(table="fish", operation="update", values={"weight_kg": 9}, where={"id": 1}),
        call(table="fish", operation="delete", where={"name": "perch"}),
        ToolCall("query_data", {"sql": "SELECT name FROM data.fish"}),
    )

    for refused in (inserted, upserted, updated):
        assert set(refused) == {"error"}
        assert refused["error"].startswith("The user's data store is full: its data takes up ")
        assert "of the 1.0 KB allowed" in refused["error"]
        assert "Reading still works, and deleting rows makes room" in refused["error"]
    assert deleted["row_count"] == 1
    assert queried["rows"] == [["pike"]]
    assert fish(store) == [("pike", "4.2", None)]


def test_deleting_rows_at_the_database_cap_lets_writes_in_again(api, db, store, agent, monkeypatch):
    """The cap is on the data the store holds, not its database's size, which keeps the space
    deleted rows took: deleting enough makes room, at once and on the Data page."""
    monkeypatch.setattr(get_settings(), "data_store_db_quota_bytes", 4 * 1024 * 1024)
    monkeypatch.setattr(get_settings(), "write_data_max_rows", 10_000)
    with as_owner(store) as conn:
        # 8 MB of notes, an eighth of them kept, all through the table.
        conn.execute("CREATE TABLE data.notes (id int PRIMARY KEY, keep boolean, body text)")
        conn.execute(
            "INSERT INTO data.notes SELECT g, g % 8 = 0,"
            " (SELECT string_agg(md5(g::text || '-' || h), '') FROM generate_series(1, 32) h)"
            " FROM generate_series(1, 8000) g"
        )
    worker.measure_data_store_usage_task.apply().get()
    assert api.get("/api/data-store").json()["usage"]["db_bytes"] > 8 * 1024 * 1024

    refused, deleted, inserted = run_tools(
        api,
        agent,
        call(table="fish", operation="insert", rows=[{"name": "carp"}]),
        call(table="notes", operation="delete", where={"keep": False}),
        call(table="fish", operation="insert", rows=[{"name": "carp"}]),
    )
    worker.measure_data_store_usage_task.apply().get()

    assert refused["error"].startswith("The user's data store is full")
    assert deleted["row_count"] == 7000
    assert inserted["row_count"] == 1
    assert [f[0] for f in fish(store)] == ["pike", "perch", "carp"]
    assert api.get("/api/data-store").json()["usage"]["db_bytes"] < 2 * 1024 * 1024


def test_a_store_at_its_file_cap_refuses_inserts_and_updates_too(
    api, store, agent, files_bucket, monkeypatch
):
    """Either cap is the store's: past it, writes stop."""
    monkeypatch.setattr(get_settings(), "data_store_files_quota_bytes", 100)
    add_document(store.id, b"x" * 100)

    inserted, deleted = run_tools(
        api,
        agent,
        call(table="fish", operation="insert", rows=[{"name": "carp"}]),
        call(table="fish", operation="delete", where={"name": "perch"}),
    )

    assert inserted == {
        "error": "The user's data store is full: its documents use 100 B of the 100 B allowed,"
        " so rows can't be inserted or updated. Reading and deleting rows still work. Tell the"
        " user: deleting documents on the Data page makes room."
    }
    assert deleted["row_count"] == 1
