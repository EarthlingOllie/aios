"""The Data page's Tables: the store's `data` tables, and their rows (`aios.api.data`).

These run against real, provisioned stores, with `data` tables made directly as the store's
owner role, as in `test_data_tools`.
"""

import json

import psycopg
import pytest
from conftest import store_of
from sqlalchemy import update
from test_data_tools import as_owner, provisioned

from aios.config import get_settings, libpq_conninfo
from aios.datastores import tables as data_tables
from aios.datastores.connections import dispose_engines
from aios.models import DataStore, DataStoreStatus


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in user's store, provisioned, with a hand-made `data.fish` table."""
    row = provisioned(db, store_of(user))
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


def test_the_tables_list_has_each_data_table_with_its_columns_and_row_count(api, store):
    response = api.get("/api/data/tables")

    assert response.status_code == 200
    assert response.json() == {
        "tables": [
            {
                "name": "fish",
                "description": "Fish the user has caught",
                "columns": [
                    {
                        "name": "id",
                        "type": "bigint",
                        "nullable": False,
                        "description": None,
                        "embedding": False,
                    },
                    {
                        "name": "name",
                        "type": "text",
                        "nullable": False,
                        "description": None,
                        "embedding": False,
                    },
                    {
                        "name": "weight_kg",
                        "type": "numeric",
                        "nullable": True,
                        "description": "As weighed on the boat",
                        "embedding": False,
                    },
                ],
                "row_count": 3,
                "row_count_estimated": False,
            }
        ],
        "truncated": False,
    }


def tables(api) -> dict:
    response = api.get("/api/data/tables")
    assert response.status_code == 200, response.text
    return {t["name"]: t for t in response.json()["tables"]}


def test_another_stores_tables_are_never_listed(api, store, db, new_user):
    other = provisioned(db, store_of(new_user()))
    with as_owner(other) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")

    assert set(tables(api)) == {"fish"}


def test_a_big_tables_row_count_is_the_planners_estimate(api, store, monkeypatch):
    """Counting a big table's rows would take as long as reading them all."""
    monkeypatch.setattr(data_tables, "EXACT_COUNT_BELOW", 1000)
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.readings (v int)")
        conn.execute("INSERT INTO data.readings SELECT generate_series(1, 5000)")
        conn.execute("ANALYZE data.readings")

    listed = tables(api)

    assert (listed["readings"]["row_count"], listed["readings"]["row_count_estimated"]) == (
        5000,
        True,
    )
    assert (listed["fish"]["row_count"], listed["fish"]["row_count_estimated"]) == (3, False)


def test_tables_past_the_byte_cap_are_left_out_and_it_says_so(api, store, monkeypatch):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.zebra (stripes int)")
    one_table = len(json.dumps(api.get("/api/data/tables").json()["tables"][0]))
    # Room for one table, give or take, not two.
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", one_table + 60)

    listed = api.get("/api/data/tables").json()

    assert [t["name"] for t in listed["tables"]] == ["fish"]
    assert listed["truncated"] is True


def test_the_tables_wait_for_the_store_to_be_ready(api, db, user):
    db.execute(
        update(DataStore)
        .where(DataStore.id == store_of(user))
        .values(status=DataStoreStatus.provisioning)
    )
    db.commit()

    response = api.get("/api/data/tables")

    assert response.status_code == 409
    assert response.json()["data_store_status"] == "provisioning"


def rows(api, table: str, **params) -> dict:
    response = api.get(f"/api/data/tables/{table}/rows", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def values(page: dict) -> list[list]:
    return [r["values"] for r in page["rows"]]


def test_a_tables_rows_come_a_page_at_a_time_sorted_by_a_column(api, store):
    first = rows(api, "fish", sort="weight_kg", order="desc", limit=2)
    second = rows(api, "fish", sort="weight_kg", order="desc", limit=2, offset=2)

    assert first["columns"] == ["id", "name", "weight_kg"]
    # As text, as Postgres writes them.
    assert values(first) == [["3", "carp", "7.5"], ["1", "pike", "4.2"]]
    assert first["has_more"] is True
    assert values(second) == [["2", "perch", "0.8"]]
    assert second["has_more"] is False
    assert rows(api, "fish", sort="name")["rows"][0]["values"][1] == "carp"


@pytest.mark.parametrize(
    "table",
    [
        "whales",
        # Names, never SQL: these name no table.
        "fish; DROP TABLE data.fish",
        'fish" --',
        "FISH",
        # Not the owner's, nor the platform's.
        "chunks",
        "aios.chunks",
        "data.fish",
    ],
)
def test_an_unknown_table_is_not_found(api, store, table):
    response = api.get(f"/api/data/tables/{table}/rows")

    assert response.status_code == 404
    assert rows(api, "fish")["columns"] == ["id", "name", "weight_kg"]


def test_another_stores_table_is_not_found(api, store, db, new_user):
    other = provisioned(db, store_of(new_user()))
    with as_owner(other) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")

    assert api.get("/api/data/tables/secrets/rows").status_code == 404


@pytest.mark.parametrize("sort", ["colour", "weight_kg DESC", "name; DROP TABLE data.fish", "1"])
def test_sorting_by_an_unknown_column_is_unprocessable(api, store, sort):
    response = api.get("/api/data/tables/fish/rows", params={"sort": sort})

    assert response.status_code == 422
    assert "no column" in response.json()["detail"]
    assert len(rows(api, "fish")["rows"]) == 3


@pytest.fixture
def notes(store) -> DataStore:
    """`data.notes`: jsonb, embeddings (a domain over one too) and wide values."""
    with as_owner(store) as conn:
        conn.execute("CREATE DOMAIN data.embedding AS vector(3)")
        conn.execute(
            "CREATE TABLE data.notes (id int PRIMARY KEY, meta jsonb, body text,"
            " vec vector(3), loose vector, small halfvec(2), sparse sparsevec(5),"
            " emb data.embedding)"
        )
        conn.execute(
            "INSERT INTO data.notes VALUES"
            """ (1, '{"b": [1, 2], "a": "x"}', 'short', '[1,2,3]', '[1,2,3,4]', '[1,2]',"""
            " '{1:1,3:2}/5', '[4,5,6]'),"
            " (2, NULL, repeat('wide ', 1000), NULL, NULL, NULL, NULL, NULL)"
        )
    return store


def test_embeddings_show_as_their_dimension_not_their_numbers(api, notes):
    listed = tables(api)["notes"]
    page = rows(api, "notes", sort="id")

    embedding = {c["name"]: c["embedding"] for c in listed["columns"]}
    assert embedding == {
        "id": False,
        "meta": False,
        "body": False,
        "vec": True,
        "loose": True,
        "small": True,
        "sparse": True,
        "emb": True,
    }
    first, second = values(page)
    assert first[3:] == ["vector(3)", "vector(4)", "halfvec(2)", "sparsevec(5)", "vector(3)"]
    assert second[3:] == [None] * 5


def test_jsonb_is_pretty_printed(api, notes):
    page = rows(api, "notes", sort="id")

    assert values(page)[0][1] == '{\n    "a": "x",\n    "b": [\n        1,\n        2\n    ]\n}'


def value(api, table: str, row: str, column: str):
    return api.get(f"/api/data/tables/{table}/value", params={"row": row, "column": column})


def test_a_wide_value_is_cut_short_and_can_be_read_in_full(api, notes):
    short, wide = rows(api, "notes", sort="id")["rows"]

    assert short["truncated"] == []
    assert wide["truncated"] == [2]
    assert wide["values"][2] == ("wide " * 1000)[: data_tables.PREVIEW_CHARS]

    full = value(api, "notes", wide["key"], "body")
    assert full.status_code == 200
    assert full.json() == {"value": "wide " * 1000, "truncated": False}


def test_a_value_too_big_to_read_whole_is_read_as_far_as_the_byte_cap(api, notes, monkeypatch):
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 800)
    wide = rows(api, "notes", sort="id")["rows"][1]

    full = value(api, "notes", wide["key"], "body").json()

    assert full["truncated"] is True
    assert ("wide " * 1000).startswith(full["value"])
    assert 0 < len(full["value"].encode()) <= 800


@pytest.mark.parametrize(
    ("text", "whole"),
    [
        # Plain text is read whole up to near the cap...
        ("abc " * 7000, True),
        # ...as is text written longer in JSON, up to half of it...
        ("line\n" * 5000, True),
        # ...and the rest as far as fits, however it's written.
        ("日本語" * 10000, False),
    ],
    ids=["ascii", "escaped", "multibyte"],
)
def test_a_value_is_read_whole_as_far_as_the_byte_cap_allows(api, notes, monkeypatch, text, whole):
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 32 * 1024)
    with as_owner(notes) as conn:
        conn.execute("UPDATE data.notes SET body = %s WHERE id = 2", (text,))
    row = rows(api, "notes", sort="id")["rows"][1]

    full = value(api, "notes", row["key"], "body").json()

    assert full["truncated"] is not whole
    assert text.startswith(full["value"])
    assert len(full["value"]) == len(text) if whole else len(full["value"]) > 1000
    assert len(json.dumps(full, ensure_ascii=False).encode()) <= 32 * 1024


def test_a_page_stops_at_the_byte_cap(api, notes, monkeypatch):
    with as_owner(notes) as conn:
        conn.execute(
            "INSERT INTO data.notes (id, body) SELECT i, 'x' FROM generate_series(3, 50) i"
        )
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 2000)

    page = rows(api, "notes", sort="id", limit=50)

    assert 0 < len(page["rows"]) < 50
    assert page["has_more"] is True
    assert len(json.dumps(page["rows"]).encode()) <= 2000 * 2


@pytest.mark.parametrize(
    ("row", "column", "status"),
    [
        (None, "colour", 422),
        ("(0,1)", "body", 422),
        ("1:(0,1); DROP TABLE data.notes", "body", 422),
        ("1:(999,1)", "body", 404),
        ("4294967296:(0,1)", "body", 422),
        ("1:(0,65536)", "body", 422),
    ],
)
def test_reading_a_value_needs_a_real_row_and_column(api, notes, row, column, status):
    real = rows(api, "notes", sort="id")["rows"][0]["key"]
    key = row or real

    response = value(api, "notes", key, column)

    assert response.status_code == status, response.text
    assert value(api, "whales", real, "body").status_code == 404


def test_a_column_that_cant_be_sorted_by_is_unprocessable(api, store):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.places (name text, spot point)")
        conn.execute("INSERT INTO data.places VALUES ('home', '(1,2)')")

    response = api.get("/api/data/tables/places/rows", params={"sort": "spot"})

    assert response.status_code == 422
    assert "can't be sorted" in response.json()["detail"]
    assert values(rows(api, "places")) == [["home", "(1,2)"]]


def test_a_column_named_like_the_reads_own_columns_sorts_by_itself(api, store):
    """The page's statement names columns of its own (the row's key, each value's text)."""
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.kv (key int, c0 int, page int)")
        conn.execute("INSERT INTO data.kv SELECT i, 13 - i, i % 2 FROM generate_series(1, 12) i")

    def first_column(**params) -> list[str]:
        return [r[0] for r in values(rows(api, "kv", **params))]

    assert first_column(sort="key", order="desc")[:3] == ["12", "11", "10"]
    assert first_column(sort="c0", order="desc")[:3] == ["1", "2", "3"]
    assert first_column(sort="page") == [
        "2",
        "4",
        "6",
        "8",
        "10",
        "12",
        "1",
        "3",
        "5",
        "7",
        "9",
        "11",
    ]


def test_a_table_with_no_columns_shows_its_rows(api, store):
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.nothing ()")
        conn.execute("INSERT INTO data.nothing DEFAULT VALUES")

    assert tables(api)["nothing"]["columns"] == []
    page = rows(api, "nothing")
    assert (page["columns"], values(page)) == ([], [[]])


def test_a_table_with_a_slash_in_its_name_opens(api, store):
    with as_owner(store) as conn:
        conn.execute('CREATE TABLE data."q1/q2" (n int)')
        conn.execute('INSERT INTO data."q1/q2" VALUES (7)')

    page = api.get("/api/data/tables/q1%2Fq2/rows")

    assert page.status_code == 200, page.text
    assert values(page.json()) == [["7"]]
    key = page.json()["rows"][0]["key"]
    assert value(api, "q1/q2", key, "n").json() == {"value": "7", "truncated": False}


def test_arrays_of_embeddings_and_domains_over_domains_show_no_numbers(api, store):
    with as_owner(store) as conn:
        conn.execute("CREATE DOMAIN data.emb AS vector(2)")
        conn.execute("CREATE DOMAIN data.emb2 AS data.emb")
        conn.execute(
            "CREATE TABLE data.multi (many vector(2)[], deep data.emb2, deeps data.emb2[])"
        )
        conn.execute(
            "INSERT INTO data.multi VALUES (ARRAY['[1,2]', '[3,4]']::vector(2)[], '[5,6]',"
            " ARRAY['[7,8]']::data.emb2[])"
        )

    listed = tables(api)["multi"]
    page = rows(api, "multi")

    assert [c["embedding"] for c in listed["columns"]] == [True, True, True]
    assert values(page) == [["2 × vector", "vector(2)", "1 × vector"]]


def test_counting_rows_has_a_time_budget_past_which_counts_are_left_out(api, store, monkeypatch):
    """A table never analyzed has no estimate, and counting it could take as long as reading
    it: the list doesn't wait for that."""
    monkeypatch.setattr(data_tables, "COUNT_BUDGET_MS", 1)
    with as_owner(store) as conn:
        conn.execute("CREATE TABLE data.big (h text) WITH (autovacuum_enabled = false)")
        conn.execute("INSERT INTO data.big SELECT md5(i::text) FROM generate_series(1, 300000) i")

    response = api.get("/api/data/tables")

    assert response.status_code == 200, response.text
    big = {t["name"]: t for t in response.json()["tables"]}["big"]
    assert big["row_count"] is None


def test_a_wide_rows_values_are_cut_shorter_so_a_page_fits_the_byte_cap(api, store, monkeypatch):
    with as_owner(store) as conn:
        columns = ", ".join(f"c{i} text" for i in range(40))
        conn.execute(f"CREATE TABLE data.wide ({columns})")
        conn.execute(f"INSERT INTO data.wide SELECT {', '.join(['repeat(chr(26085), 300)'] * 40)}")
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 16 * 1024)

    page = rows(api, "wide")

    assert len(page["rows"]) == 1
    assert page["has_more"] is False
    assert page["rows"][0]["truncated"] == list(range(40))


def test_a_row_too_wide_to_show_at_all_says_so(api, store, monkeypatch):
    with as_owner(store) as conn:
        columns = ", ".join(f"c{i} text" for i in range(40))
        conn.execute(f"CREATE TABLE data.wide ({columns})")
        conn.execute(f"INSERT INTO data.wide SELECT {', '.join(['repeat(chr(26085), 300)'] * 40)}")
    monkeypatch.setattr(get_settings(), "query_data_max_bytes", 1000)

    response = api.get("/api/data/tables/wide/rows")

    assert response.status_code == 422
    assert "too wide" in response.json()["detail"]


def test_a_read_past_the_time_limit_is_stopped(api, store):
    with as_owner(store) as conn:
        conn.execute(
            "CREATE TABLE data.big AS SELECT md5(i::text) AS h FROM generate_series(1, 300000) i"
        )
    with psycopg.connect(
        libpq_conninfo(get_settings().data_store_provisioner_url), autocommit=True
    ) as conn:
        conn.execute(f'ALTER ROLE "{store.role_prefix}_ro" SET statement_timeout = 30')
    dispose_engines()

    response = api.get("/api/data/tables/big/rows", params={"sort": "h"})

    assert response.status_code == 504
    assert "too long" in response.json()["detail"]


def test_the_tables_api_takes_names_and_numbers_never_sql(client):
    """Each parameter is a name checked against the catalog, a number, or a direction."""
    schema = client.get("/openapi.json").json()
    data_paths = {p: ops for p, ops in schema["paths"].items() if p.startswith("/api/data/")}

    params = {
        (op_name, param["name"])
        for ops in data_paths.values()
        for op_name, op in ops.items()
        for param in op.get("parameters", [])
    }

    assert set(data_paths) == {
        "/api/data/tables",
        "/api/data/tables/{table}/rows",
        "/api/data/tables/{table}/value",
    }
    assert all(op == "get" for op, _ in params)
    assert {name for _, name in params} == {
        "table",
        "offset",
        "limit",
        "sort",
        "order",
        "row",
        "column",
    }
    assert not any("requestBody" in op for ops in data_paths.values() for op in ops.values())
