"""Reversible drops on a real, provisioned store (`aios.datastores.dropped`): an approved
`drop_table` or `drop_column` renames the table or column out of the way and takes the data
tools' access to it, while its data stays; the owner can restore it until it's purged."""

from datetime import datetime, timedelta

import pytest
from conftest import store_of
from fake_model import ToolCall, model, use_model
from sqlalchemy import update
from test_agents import agent_body, make_agent
from test_data_tools import as_owner, provisioned, run_tools
from test_schema_change_requests import TOOLS as REQUEST_TOOLS
from test_schema_change_requests import Script, run_queued, start

from aios import worker
from aios.auth import current_user
from aios.config import get_settings
from aios.datastores import dropped, schema_changes
from aios.datastores.schema_changes import SchemaChange
from aios.main import app
from aios.models import DataStore, SchemaChangeRequest, SchemaChangeStatus
from aios.tools import build_tool

TOOLS = [
    {"key": "describe_data", "config": {}},
    {"key": "query_data", "config": {}},
    {"key": "write_data", "config": {}},
]


@pytest.fixture
def store(db, user) -> DataStore:
    """The organisation's store, with `data.fish` (two fish) made by an applied request, as agents'
    tables are."""
    row = provisioned(db, store_of(user))
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


# --- Approving ----------------------------------------------------------------------------------


@pytest.fixture
def queued(monkeypatch) -> list:
    """The schema changes queued to be applied, instead of sent to the broker."""
    applies: list = []
    monkeypatch.setattr(worker.apply_schema_change_task, "delay", applies.append)
    return applies


def test_approving_a_drop_takes_the_tables_names_typed(api, store, queued):
    request = propose(
        store,
        {"op": "drop_column", "table": "fish", "column": "weight_kg"},
        {"op": "create_table", "name": "eels", "columns": [{"name": "x", "type": "text"}]},
    )
    url = f"/api/schema-changes/{request.id}"
    assert api.get(url).json()["confirm_with"] == "fish"

    for body in (None, {}, {"confirm": ""}, {"confirm": "eels"}, {"confirm": "Fish"}):
        r = api.post(f"{url}/approve", json=body)
        assert r.status_code == 422, (body, r.text)
        assert "Type `fish` to confirm" in r.json()["detail"]
    assert api.get(url).json()["status"] == "pending"
    assert queued == []

    r = api.post(f"{url}/approve", json={"confirm": " fish "})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert queued == [str(request.id)]


def test_a_request_that_drops_nothing_needs_no_confirmation(api, store, queued):
    request = propose(
        store, {"op": "add_column", "table": "fish", "column": {"name": "x", "type": "text"}}
    )

    assert api.get(f"/api/schema-changes/{request.id}").json()["confirm_with"] is None
    assert api.post(f"/api/schema-changes/{request.id}/approve").status_code == 200


def test_what_a_drop_would_remove_is_shown_before_its_approved(api, store):
    request = propose(
        store,
        {"op": "drop_column", "table": "fish", "column": "weight_kg"},
        {"op": "drop_table", "table": "fish"},
    )
    with as_owner(store) as conn:
        conn.execute("INSERT INTO data.fish (name) VALUES ('eel')")

    r = api.get(f"/api/schema-changes/{request.id}/impact")

    assert r.status_code == 200, r.text
    assert r.json() == {
        "removes": [
            {
                "table": "fish",
                "column": "weight_kg",
                "rows": 3,
                "values": 2,
                "columns": None,
                "estimated": False,
            },
            {
                "table": "fish",
                "column": None,
                "rows": 3,
                "values": None,
                "columns": ["id", "name", "weight_kg"],
                "estimated": False,
            },
        ]
    }


def test_the_tool_offers_drops_and_says_they_can_be_undone():
    [request_tool] = build_tool("request_schema_change", {})

    assert "drop_table" in request_tool.description
    assert "drop_column" in request_tool.description
    assert "restore" in request_tool.description


def test_an_agent_asks_from_its_chat_to_drop_a_table_and_is_told_it_can_be_restored(
    api, store, monkeypatch
):
    tasks: list = []
    for task in (worker.apply_schema_change_task, worker.resume_agent_run_task):
        monkeypatch.setattr(task, "delay", lambda *args, task=task: tasks.append((task, args)))
    agent = make_agent(api, "Tidier")
    agent = api.put(
        f"/api/agents/{agent['id']}", json=agent_body(agent, tools=REQUEST_TOOLS)
    ).json()
    script = Script({"op": "drop_table", "table": "fish"})

    run, request = start(api, agent, script)
    assert request["confirm_with"] == "fish"
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 422
    r = api.post(f"/api/schema-changes/{request['id']}/approve", json={"confirm": "fish"})
    assert r.status_code == 200, r.text
    with use_model(model(script)):
        run_queued(tasks)

    (_, outcome), (_, described) = script.results
    assert outcome["status"] == "applied"
    assert "restore" in outcome["message"]
    assert "fish" not in {t["name"] for t in described["tables"]}


def test_a_count_that_takes_too_long_is_estimated_instead(api, store, monkeypatch):
    """A huge table, or one locked by a change being applied, mustn't hold up the card."""
    monkeypatch.setattr(dropped, "IMPACT_COUNT_TIMEOUT", 200)
    request = propose(store, {"op": "drop_table", "table": "fish"})

    with as_owner(store) as conn, conn.transaction():
        conn.execute("ANALYZE data.fish")
        conn.execute("LOCK TABLE data.fish IN ACCESS EXCLUSIVE MODE")
        r = api.get(f"/api/schema-changes/{request.id}/impact")

    assert r.status_code == 200, r.text
    [removal] = r.json()["removes"]
    assert (removal["rows"], removal["estimated"]) == (2, True)
    assert removal["columns"] == ["id", "name", "weight_kg"]


# --- The Dropped section, and restoring ---------------------------------------------------------


@pytest.fixture
def restores(monkeypatch) -> list:
    """The restores queued, as (store id, dropped id), instead of sent to the broker."""
    queued: list = []
    monkeypatch.setattr(worker.restore_dropped_task, "delay", lambda *a: queued.append(a))
    return queued


def run_restores(restores: list) -> None:
    while restores:
        worker.restore_dropped_task.run(*restores.pop(0))


def listed(api) -> list[dict]:
    r = api.get("/api/dropped")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_dropped_section_lists_what_was_dropped_with_when_its_purged(
    db, api, store, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 2)
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    apply(db, store, {"op": "drop_table", "table": "fish"})

    [table] = listed(api)  # the column went with its table: it's not listed by itself
    assert (table["kind"], table["table"], table["column"]) == ("table", "fish", None)
    dropped_at = datetime.fromisoformat(table["dropped_at"])
    assert datetime.fromisoformat(table["purge_at"]) - dropped_at == timedelta(days=2)
    assert table["blocked"] is None and table["restore_error"] is None


def test_restoring_a_dropped_table_brings_it_back_intact(db, api, agent, store, restores):
    with as_owner(store) as conn:
        conn.execute("COMMENT ON TABLE data.fish IS 'Fish caught'")
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    apply(db, store, {"op": "drop_table", "table": "fish"})
    [table] = listed(api)

    r = api.post(f"/api/dropped/{table['id']}/restore")
    assert r.status_code == 202, r.text
    run_restores(restores)

    described = tool(api, agent, "describe_data")
    [fish] = [t for t in described["tables"] if t["schema"] == "data"]
    assert (fish["name"], fish["description"]) == ("fish", "Fish caught")
    # Its dropped column is back too, still dropped, and listed by itself again.
    assert [c["name"] for c in fish["columns"]] == ["id", "name"]
    [column] = listed(api)
    assert (column["kind"], column["table"], column["column"]) == ("column", "fish", "weight_kg")
    read = tool(api, agent, "query_data", sql="SELECT id, name FROM data.fish ORDER BY id")
    assert read["rows"] == [[1, "pike"], [2, "perch"]]
    with as_owner(store) as conn:
        [(summary,)] = conn.execute(
            "SELECT summary FROM aios.schema_history ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert summary == "Restore table data.fish"


def test_restoring_a_dropped_column_brings_back_its_values_and_constraint(
    db, api, agent, store, restores
):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    [column] = listed(api)

    assert api.post(f"/api/dropped/{column['id']}/restore").status_code == 202
    run_restores(restores)

    assert listed(api) == []
    # The table is whole again: `SELECT *` reads it.
    read = tool(api, agent, "query_data", sql="SELECT * FROM data.fish ORDER BY id")
    assert read["columns"] == ["id", "name", "weight_kg"]
    assert [row[1] for row in read["rows"]] == ["pike", "perch"]
    with as_owner(store) as conn:
        [(not_null,)] = conn.execute(
            "SELECT attnotnull FROM pg_attribute"
            " WHERE attrelid = 'data.fish'::regclass AND attname = 'name'"
        ).fetchall()
    assert not_null


def test_a_restored_column_that_rows_left_empty_meanwhile_comes_back_nullable(
    db, api, agent, store, restores
):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    tool(api, agent, "write_data", table="fish", operation="insert", rows=[{"weight_kg": 1}])
    [column] = listed(api)

    api.post(f"/api/dropped/{column['id']}/restore")
    run_restores(restores)

    assert listed(api) == []
    read = tool(api, agent, "query_data", sql="SELECT name FROM data.fish ORDER BY id")
    assert read["rows"] == [["pike"], ["perch"], [None]]


def test_restoring_onto_a_name_taken_since_is_refused(db, api, store, restores):
    apply(db, store, {"op": "drop_table", "table": "fish"})
    apply(
        db,
        store,
        {"op": "create_table", "name": "fish", "columns": [{"name": "x", "type": "text"}]},
    )
    [table] = listed(api)
    assert "`data.fish` exists again" in table["blocked"]

    r = api.post(f"/api/dropped/{table['id']}/restore")

    assert r.status_code == 409
    assert "`data.fish` exists again" in r.json()["detail"]
    assert restores == []


def test_a_restore_the_name_was_taken_before_it_ran_is_refused_and_says_why(
    db, api, store, restores
):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    [column] = listed(api)
    assert api.post(f"/api/dropped/{column['id']}/restore").status_code == 202
    apply(
        db, store, {"op": "add_column", "table": "fish", "column": {"name": "name", "type": "text"}}
    )

    run_restores(restores)

    [still] = listed(api)
    assert "already has a column `name`" in still["restore_error"]
    with as_owner(store) as conn:
        assert (
            conn.execute(f'SELECT count("{still["dropped_name"]}") FROM data.fish').fetchone()[0]
            == 2
        )


def test_a_dropped_tables_column_is_restored_with_the_table_not_by_itself(db, api, store):
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    [column] = listed(api)
    apply(db, store, {"op": "drop_table", "table": "fish"})

    r = api.post(f"/api/dropped/{column['id']}/restore")

    assert r.status_code == 409
    assert "Restore the table `fish` first" in r.json()["detail"]


def test_embedding_columns_stay_marked_through_a_drop_and_a_restore(db, api, store, restores):
    bio = {"name": "bio", "type": "text"}
    embedding = {"name": "bio_embedding", "type": "embedding", "embedding_of": "bio"}
    apply(db, store, {"op": "create_table", "name": "people", "columns": [bio, embedding]})

    def markers() -> list[tuple]:
        with as_owner(store) as conn:
            return conn.execute(
                "SELECT table_name, column_name FROM aios.embedding_columns ORDER BY 1, 2"
            ).fetchall()

    dropped_as = apply(db, store, {"op": "drop_table", "table": "people"}).ops[0]["dropped_as"]
    assert markers() == [(dropped_as, "bio_embedding")]
    # The name is free for a new table with an embedding column of the same name.
    apply(db, store, {"op": "create_table", "name": "people", "columns": [bio, embedding]})
    apply(db, store, {"op": "drop_table", "table": "people"})
    [newer, older] = listed(api)
    api.post(f"/api/dropped/{older['id']}/restore")
    run_restores(restores)

    assert [m for m in markers() if m[0] == "people"] == [("people", "bio_embedding")]


def test_a_store_that_cant_be_read_just_now_says_so_rather_than_nothing_is_dropped(
    db, api, store, restores
):
    apply(db, store, {"op": "drop_table", "table": "fish"})
    [table] = listed(api)
    # Behind the store tree's head, as during a deploy: worth trying again shortly.
    db.execute(update(DataStore).where(DataStore.id == store.id).values(schema_revision="0004"))
    db.commit()

    assert api.get("/api/dropped").status_code == 503
    assert api.post(f"/api/dropped/{table['id']}/restore").status_code == 503
    assert restores == []


def test_only_the_stores_owner_sees_and_restores_its_dropped_objects(
    db, api, store, new_user, restores
):
    apply(db, store, {"op": "drop_table", "table": "fish"})
    [table] = listed(api)
    other = new_user()
    app.dependency_overrides[current_user] = lambda: other

    assert listed(api) == []
    assert api.post(f"/api/dropped/{table['id']}/restore").status_code == 404
    assert restores == []


# --- Purging ------------------------------------------------------------------------------------


def age(store: DataStore, dropped_name: str, days: float) -> None:
    """Make a dropped table or column look `days` old."""
    with as_owner(store) as conn:
        conn.execute(
            "UPDATE aios.dropped_objects SET dropped_at = now() - make_interval(secs => %s)"
            " WHERE dropped_name = %s",
            (days * 86400, dropped_name),
        )


def test_the_purge_removes_only_what_is_past_the_retention_period(
    db, api, agent, store, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 1)
    eels = {"op": "create_table", "name": "eels", "columns": [{"name": "x", "type": "text"}]}
    apply(db, store, eels)
    column = apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    old_table = apply(db, store, {"op": "drop_table", "table": "eels"})
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    for request in (column, old_table):
        age(store, request.ops[0]["dropped_as"], 1.01)

    purged = dropped.purge(store.id)

    assert purged == ["Purge dropped table data.eels", "Purge dropped column name of data.fish"]
    [kept] = listed(api)
    assert (kept["table"], kept["column"]) == ("fish", "weight_kg")
    with as_owner(store) as conn:
        columns = conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'data.fish'::regclass"
            " AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        ).fetchall()
        assert [c for (c,) in columns] == ["id", kept["dropped_name"]]
        tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'data'")
        assert [t for (t,) in tables] == ["fish"]
        summaries = conn.execute(
            "SELECT summary FROM aios.schema_history ORDER BY id DESC LIMIT 2"
        ).fetchall()
    assert [s for (s,) in summaries] == purged[::-1]
    # Nothing's left to purge yet.
    assert dropped.purge(store.id) == []


def test_once_a_tables_last_dropped_column_is_purged_select_star_reads_it_again(
    db, api, agent, store, monkeypatch
):
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 0)
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})

    dropped.purge(store.id)

    read = tool(api, agent, "query_data", sql="SELECT * FROM data.fish ORDER BY id")
    assert read["columns"] == ["id", "name"]


def test_a_dropped_tables_dropped_columns_go_with_it(db, api, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 0)
    apply(db, store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    apply(db, store, {"op": "drop_table", "table": "fish"})

    assert dropped.purge(store.id) == ["Purge dropped table data.fish"]

    assert listed(api) == []
    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM aios.dropped_objects").fetchone() == (0,)


def test_the_scheduled_purge_leaves_a_store_behind_the_trees_head_for_later(db, store, monkeypatch):
    """Before it's migrated it may not have `aios.dropped_objects` yet."""
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 0)
    apply(db, store, {"op": "drop_table", "table": "fish"})
    db.execute(update(DataStore).where(DataStore.id == store.id).values(schema_revision="0004"))
    db.commit()

    assert store.id not in dropped.purge_all()

    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM aios.dropped_objects").fetchone() == (1,)


def test_the_purge_is_scheduled_on_the_provisioning_worker():
    scheduled = [e["task"] for e in worker.celery_app.conf.beat_schedule.values()]

    assert "aios.purge_dropped" in scheduled
    assert worker.celery_app.conf.task_routes["aios.purge_dropped"] == {"queue": "provisioning"}


def test_the_scheduled_purge_goes_through_every_ready_store(db, store, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_store_dropped_retention_days", 0)
    apply(db, store, {"op": "drop_table", "table": "fish"})

    worker.purge_dropped_task.run()

    with as_owner(store) as conn:
        assert conn.execute("SELECT count(*) FROM aios.dropped_objects").fetchone() == (0,)


def test_the_tables_page_leaves_out_a_dropped_table(db, api, store):
    apply(db, store, {"op": "drop_table", "table": "fish"})

    listed = api.get("/api/data/tables").json()

    assert listed["tables"] == []
    assert api.get("/api/data/tables/fish/rows").status_code == 404


def test_the_tables_page_shows_a_table_with_a_dropped_column_without_it(db, api, store):
    request = apply(db, store, {"op": "drop_column", "table": "fish", "column": "name"})
    dropped_as = request.ops[0]["dropped_as"]

    [fish] = api.get("/api/data/tables").json()["tables"]
    page = api.get("/api/data/tables/fish/rows", params={"sort": "id"})

    assert fish["name"] == "fish"
    assert [c["name"] for c in fish["columns"]] == ["id", "weight_kg"]
    assert page.status_code == 200, page.text
    assert page.json()["columns"] == ["id", "weight_kg"]
    assert [r["values"] for r in page.json()["rows"]] == [["1", "4.2"], ["2", "0.8"]]
    row = page.json()["rows"][0]["key"]
    for column in ("name", dropped_as):
        value = api.get("/api/data/tables/fish/value", params={"row": row, "column": column})
        assert value.status_code == 422, value.text
