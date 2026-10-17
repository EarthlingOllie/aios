"""Reversible drops on a real, provisioned store (`aios.datastores.dropped`): an approved
`drop_table` or `drop_column` renames the table or column out of the way and takes the data
tools' access to it, while its data stays; the owner can restore it until it's purged."""

import pytest
from fake_model import ToolCall
from sqlalchemy import update
from test_agents import agent_body, make_agent
from test_data_tools import as_owner, provisioned, run_tools

from aios.datastores import data_store_for, schema_changes
from aios.datastores.schema_changes import SchemaChange
from aios.models import DataStore, SchemaChangeRequest, SchemaChangeStatus

TOOLS = [
    {"key": "describe_data", "config": {}},
    {"key": "query_data", "config": {}},
    {"key": "write_data", "config": {}},
]


@pytest.fixture
def store(db, user) -> DataStore:
    """The user's store, with `data.fish` (two fish) made by an applied request, as agents'
    tables are."""
    row = provisioned(db, data_store_for(user.id))
    apply(
        db,
        row,
        {
            "op": "create_table",
            "name": "fish",
            "columns": [
                {"name": "name", "type": "text", "nullable": False},
                {"name": "weight_kg", "type": "numeric"},
            ],
        },
    )
    with as_owner(row) as conn:
        conn.execute("INSERT INTO data.fish (name, weight_kg) VALUES ('pike', 4.2), ('perch', 0.8)")
    return row


@pytest.fixture
def agent(api) -> dict:
    agent = make_agent(api, "Keeper")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def propose(store: DataStore, *ops: dict) -> SchemaChangeRequest:
    spec = SchemaChange.model_validate({"ops": list(ops)})
    return schema_changes.propose(store.id, spec, "Tidy up", run_id=None, tool_call_id=None)


def apply(db, store: DataStore, *ops: dict) -> SchemaChangeRequest:
    """Propose, approve and apply a request, as the owner approving it would."""
    request = propose(store, *ops)
    db.execute(
        update(SchemaChangeRequest)
        .where(SchemaChangeRequest.id == request.id)
        .values(status=SchemaChangeStatus.approved)
    )
    db.commit()
    schema_changes.apply(request.id)
    db.expire_all()
    applied = db.get(SchemaChangeRequest, request.id)
    assert applied.status == SchemaChangeStatus.applied, applied.error
    return applied


def tool(api, agent, name: str, **args) -> dict:
    return run_tools(api, agent, ToolCall(name, args))[0]


def data_tables(api, agent) -> dict[str, list[str]]:
    """What `describe_data` shows of `data`: each table's columns."""
    described = tool(api, agent, "describe_data")
    return {
        t["name"]: [c["name"] for c in t["columns"]]
        for t in described["tables"]
        if t["schema"] == "data"
    }


def test_a_dropped_table_is_gone_from_the_data_tools_but_its_rows_stay(db, api, store, agent):
    request = apply(db, store, {"op": "drop_table", "table": "fish"})
    [op] = request.ops
    dropped_as = op["dropped_as"]

    assert data_tables(api, agent) == {}
    assert "error" in tool(api, agent, "query_data", sql="SELECT * FROM data.fish")
    refused = tool(api, agent, "query_data", sql=f'SELECT * FROM data."{dropped_as}"')
    assert "permission denied" in refused["error"]
    written = tool(api, agent, "write_data", table="fish", operation="insert", rows=[{"name": "x"}])
    assert "no table `fish` in `data` (its tables: none yet)" in written["error"]
    with as_owner(store) as conn:
        assert conn.execute(f'SELECT name FROM data."{dropped_as}" ORDER BY id').fetchall() == [
            ("pike",),
            ("perch",),
        ]
        [(summary, request_id)] = conn.execute(
            "SELECT summary, request_id FROM aios.schema_history ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert (summary, request_id) == ("Drop table data.fish", request.id)
        assert conn.execute(
            "SELECT table_name, column_name, dropped_name FROM aios.dropped_objects"
        ).fetchall() == [("fish", None, dropped_as)]


def test_a_dropped_column_is_gone_from_the_data_tools_but_its_values_stay(db, api, store, agent):
    request = apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    dropped_as = request.ops[0]["dropped_as"]

    assert data_tables(api, agent) == {"fish": ["id", "weight_kg"]}
    gone = tool(api, agent, "query_data", sql="SELECT name FROM data.fish")
    assert 'column "name" does not exist' in gone["error"]
    renamed = tool(api, agent, "query_data", sql=f'SELECT "{dropped_as}" FROM data.fish')
    assert "permission denied" in renamed["error"]
    read = tool(
        api, agent, "query_data", sql="SELECT id, weight_kg::text FROM data.fish ORDER BY id"
    )
    assert read["rows"] == [[1, "4.2"], [2, "0.8"]]
    assert tool(api, agent, "query_data", sql="SELECT count(*) FROM data.fish")["rows"] == [[2]]
    # `SELECT *` would read it: refused, and the model is told what to do instead.
    star = tool(api, agent, "query_data", sql="SELECT * FROM data.fish")
    assert "name the columns" in star["error"]
    # It was NOT NULL: new rows leave it out.
    written = tool(
        api, agent, "write_data", table="fish", operation="insert", rows=[{"weight_kg": 1}]
    )
    assert "error" not in written, written
    refused = tool(
        api, agent, "write_data", table="fish", operation="insert", rows=[{"name": "eel"}]
    )
    assert "has no column `name`" in refused["error"]
    assert "_dropped_" not in refused["error"]  # nor lists it among those it has
    with as_owner(store) as conn:
        assert conn.execute(f'SELECT "{dropped_as}" FROM data.fish ORDER BY id').fetchall() == [
            ("pike",),
            ("perch",),
            (None,),
        ]


def test_a_column_added_beside_a_dropped_one_can_be_read_and_written(db, api, store, agent):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    apply(
        db,
        store,
        {"op": "add_column", "table": "fish", "column": {"name": "name", "type": "text"}},
    )

    assert data_tables(api, agent) == {"fish": ["id", "weight_kg", "name"]}
    written = tool(
        api, agent, "write_data", table="fish", operation="insert", rows=[{"name": "eel"}]
    )
    assert "error" not in written, written
    read = tool(api, agent, "query_data", sql="SELECT name FROM data.fish ORDER BY id")
    assert read["rows"] == [[None], [None], ["eel"]]


def test_a_table_with_a_dropped_column_still_takes_every_kind_of_write(db, api, store, agent):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})

    for args in (
        {"operation": "upsert", "rows": [{"id": 1, "weight_kg": 5}, {"id": 9, "weight_kg": 1}]},
        {"operation": "update", "values": {"weight_kg": 2}, "where": {"id": 2}},
        {"operation": "delete", "where": {"id": 9}},
    ):
        written = tool(api, agent, "write_data", table="fish", **args)
        assert "error" not in written, (args, written)
    read = tool(api, agent, "query_data", sql="SELECT id, weight_kg FROM data.fish ORDER BY id")
    assert read["rows"] == [[1, "5"], [2, "2"]]


def test_a_drop_whose_table_has_changed_since_it_was_asked_for_is_stale(db, store):
    """The SQL approved must be what runs: one that would run differently now isn't run."""
    request = propose(store, {"op": "drop_table", "table": "fish"})
    # Meanwhile, the table gets a dropped column, which the drop would have to carry along.
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    db.execute(
        update(SchemaChangeRequest)
        .where(SchemaChangeRequest.id == request.id)
        .values(status=SchemaChangeStatus.approved)
    )
    db.commit()

    assert schema_changes.apply(request.id) == SchemaChangeStatus.stale
    with as_owner(store) as conn:
        assert conn.execute("SELECT to_regclass('data.fish') IS NOT NULL").fetchone()[0]


def test_the_model_cant_name_what_a_drop_renames_to(db, store):
    request = propose(
        store, {"op": "drop_table", "table": "fish", "dropped_as": "_dropped_19700101000000_x"}
    )

    assert request.ops[0]["dropped_as"].endswith("_fish")
