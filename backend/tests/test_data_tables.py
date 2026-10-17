"""The Data page's Tables: the store's `data` tables, and their rows (`aios.api.data`).

These run against real, provisioned stores, with `data` tables made directly as the store's
owner role, as in `test_data_tools`.
"""

import json

import pytest
from sqlalchemy import update
from test_data_tools import as_owner, provisioned

from aios.config import get_settings
from aios.datastores import data_store_for
from aios.datastores import tables as data_tables
from aios.models import DataStore, DataStoreStatus


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
    other = provisioned(db, data_store_for(new_user().id))
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
        .where(DataStore.owner_user_id == user.id)
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
    other = provisioned(db, data_store_for(new_user().id))
    with as_owner(other) as conn:
        conn.execute("CREATE TABLE data.secrets (code text)")

    assert api.get("/api/data/tables/secrets/rows").status_code == 404


@pytest.mark.parametrize("sort", ["colour", "weight_kg DESC", "name; DROP TABLE data.fish", "1"])
def test_sorting_by_an_unknown_column_is_unprocessable(api, store, sort):
    response = api.get("/api/data/tables/fish/rows", params={"sort": sort})

    assert response.status_code == 422
    assert "no column" in response.json()["detail"]
    assert len(rows(api, "fish")["rows"]) == 3
