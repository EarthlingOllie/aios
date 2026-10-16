"""The agent tool that writes to the data store: `write_data` (`aios.tools.write_data`).

Like `test_data_tools`, these run against real, provisioned stores, and make `data` tables
directly as the store's owner role (standing in for approved schema changes).
"""

import pytest
from fake_model import ToolCall
from test_agents import agent_body, make_agent
from test_data_tools import as_owner, run_tools
from test_provisioning import store_row

from aios.datastores import data_store_for
from aios.models import DataStore

TOOLS = [{"key": "write_data", "config": {}}, {"key": "query_data", "config": {}}]


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in user's store, with a hand-made `data.fish` table holding two fish."""
    row = store_row(db, data_store_for(user.id))
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


def fish(store: DataStore) -> list[tuple]:
    with as_owner(store) as conn:
        return conn.execute(
            "SELECT name, weight_kg::text, caught_on::text FROM data.fish ORDER BY id"
        ).fetchall()


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
