"""Table ownership, sharing and transfer (`aios.datastores.ownership`): the agent whose schema
change made a table owns it, and only it and the agents it's shared with may write it with
`write_data`; every agent still reads every table. Editors and admins share, unshare and
transfer tables on the Data page; an archived owner leaves its tables read-only.

Like `test_write_data`, these run against real, provisioned stores."""

import pytest
from conftest import StoreRows, delete_user, join_organisation, org_of, store_of
from sqlalchemy import select, update
from test_agents import agent_body, make_agent
from test_audit_log import entries
from test_chats import signed_in_as
from test_data_tools import as_owner, provisioned, run_tools
from test_permissions import set_role

from aios.datastores import data_store_for, dropped, ownership, schema_changes, store_session
from aios.datastores.models import SchemaChangeRequest
from aios.datastores.schema_changes import SchemaChange
from aios.models import DataStore, Role, SchemaChangeStatus, User
from aios.testing.fake_model import ToolCall

TOOLS = [{"key": "write_data", "config": {}}, {"key": "query_data", "config": {}}]


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in admin's store, with a hand-made, unowned `data.fish` table."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.fish (id int PRIMARY KEY, name text)")
        conn.execute("INSERT INTO data.fish VALUES (1, 'pike')")
    return row


def writer(api, name: str) -> dict:
    agent = make_agent(api, name)
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=TOOLS)).json()


def create_table(api, store: DataStore, agent: dict, name: str) -> None:
    """`data.<name>`, made by an approved schema change the agent asked for in a run of its."""
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    spec = SchemaChange.model_validate(
        {
            "ops": [
                {
                    "op": "create_table",
                    "name": name,
                    "columns": [{"name": "note", "type": "text"}],
                }
            ]
        }
    )
    request = schema_changes.propose(store.id, spec, "Notes", run_id=run["id"], tool_call_id="c1")
    StoreRows(store.id).execute(
        update(SchemaChangeRequest)
        .where(SchemaChangeRequest.id == request.id)
        .values(status=SchemaChangeStatus.approved)
    )
    assert schema_changes.apply(store.id, request.id) == SchemaChangeStatus.applied


def listed(api) -> dict[str, dict]:
    resp = api.get("/api/data/tables")
    assert resp.status_code == 200, resp.text
    return {t["name"]: t for t in resp.json()["tables"]}


def who(agent: dict, archived: bool = False) -> dict:
    return {"id": agent["id"], "name": agent["name"], "archived": archived}


def write(api, agent: dict, table: str, **row) -> dict:
    [result] = run_tools(
        api, agent, ToolCall("write_data", {"table": table, "operation": "insert", "rows": [row]})
    )
    return result


def test_the_agent_whose_schema_change_made_a_table_owns_it(api, store):
    scribe = writer(api, "Scribe")

    create_table(api, store, scribe, "notes")

    tables = listed(api)
    assert tables["notes"]["owner"] == who(scribe)
    assert tables["notes"]["shares"] == []
    assert tables["notes"]["pending"] is False
    # A table made before owners has none.
    assert tables["fish"]["owner"] is None


def test_only_the_owner_writes_an_owned_table_and_others_are_told_who_owns_it(api, store):
    scribe, other = writer(api, "Scribe"), writer(api, "Other")
    create_table(api, store, scribe, "notes")

    assert write(api, scribe, "notes", note="mine")["row_count"] == 1
    refused = write(api, other, "notes", note="theirs")

    assert "owned by the agent Scribe" in refused["error"]
    assert "Nothing was written" in refused["error"]
    with as_owner(store) as conn:
        assert conn.execute("SELECT note FROM data.notes").fetchall() == [("mine",)]


def test_an_unowned_table_is_written_by_any_agent(api, store):
    for id_, name in ((2, "One"), (3, "Two")):
        assert write(api, writer(api, name), "fish", id=id_, name=name)["row_count"] == 1


def test_every_agent_still_reads_every_table(api, store):
    scribe, other = writer(api, "Scribe"), writer(api, "Other")
    create_table(api, store, scribe, "notes")
    write(api, scribe, "notes", note="mine")

    [read] = run_tools(api, other, ToolCall("query_data", {"sql": "SELECT note FROM data.notes"}))

    assert read["rows"] == [["mine"]]


@pytest.fixture
def editor(db, user):
    return join_organisation(db, user, "editor")


def set_owner(api, table: str, agent: dict | None):
    return api.put(f"/api/data/tables/{table}/owner", json={"agent_id": agent and agent["id"]})


def share(api, table: str, agent: dict):
    return api.post(f"/api/data/tables/{table}/shares", json={"agent_id": agent["id"]})


def unshare(api, table: str, agent: dict):
    return api.delete(f"/api/data/tables/{table}/shares/{agent['id']}")


def test_a_shared_agent_writes_until_its_unshared(api, store):
    scribe, helper = writer(api, "Scribe"), writer(api, "Helper")
    create_table(api, store, scribe, "notes")

    resp = share(api, "notes", helper)

    assert resp.status_code == 201, resp.text
    assert resp.json() == {
        "name": "notes",
        "owner": who(scribe),
        "shares": [who(helper)],
        "pending": False,
    }
    assert listed(api)["notes"]["shares"] == [who(helper)]
    assert write(api, helper, "notes", note="helped")["row_count"] == 1

    resp = unshare(api, "notes", helper)

    assert resp.status_code == 200, resp.text
    assert resp.json()["shares"] == []
    assert "owned by the agent Scribe" in write(api, helper, "notes", note="again")["error"]
    assert unshare(api, "notes", helper).status_code == 404


def test_transferring_a_table_moves_who_may_write_it(api, store):
    scribe, heir = writer(api, "Scribe"), writer(api, "Heir")
    create_table(api, store, scribe, "notes")
    share(api, "notes", heir)

    resp = set_owner(api, "notes", heir)

    assert resp.status_code == 200, resp.text
    # The heir owns it now, so its share has gone.
    assert (resp.json()["owner"], resp.json()["shares"]) == (who(heir), [])
    assert write(api, heir, "notes", note="mine now")["row_count"] == 1
    assert "owned by the agent Heir" in write(api, scribe, "notes", note="old")["error"]


def test_an_editor_assigns_an_owner_to_an_unowned_table_and_shares_it(api, store, editor):
    keeper, other = writer(api, "Keeper"), writer(api, "Other")

    with signed_in_as(editor):
        assert set_owner(api, "fish", keeper).status_code == 200
        assert share(api, "fish", other).status_code == 201
        assert unshare(api, "fish", other).status_code == 200

    assert listed(api)["fish"]["owner"] == who(keeper)
    assert "owned by the agent Keeper" in write(api, other, "fish", id=2, name="eel")["error"]


def test_transferring_or_clearing_an_owner_follows_the_approval_rules(api, store, editor):
    scribe, heir = writer(api, "Scribe"), writer(api, "Heir")
    create_table(api, store, scribe, "notes")

    # Changing an owner needs an admin by default.
    with signed_in_as(editor):
        for to in (heir, None):
            resp = set_owner(api, "notes", to)
            assert resp.status_code == 403, resp.text
            assert "needs an admin" in resp.json()["detail"]
    assert listed(api)["notes"]["owner"] == who(scribe)

    # The admin clears it: any agent writes it again.
    resp = set_owner(api, "notes", None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner"] is None
    assert listed(api)["notes"]["owner"] is None
    assert write(api, heir, "notes", note="anyone")["row_count"] == 1


def test_participants_cant_change_access(api, db, store, editor):
    agent = writer(api, "Scribe")
    set_role(db, editor, Role.participant)

    with signed_in_as(editor):
        assert set_owner(api, "fish", agent).status_code == 403
        assert share(api, "fish", agent).status_code == 403
        assert unshare(api, "fish", agent).status_code == 403


def test_each_change_is_audited(api, user, store):
    scribe, heir, helper = writer(api, "Scribe"), writer(api, "Heir"), writer(api, "Helper")

    set_owner(api, "fish", scribe)
    share(api, "fish", helper)
    unshare(api, "fish", helper)
    set_owner(api, "fish", heir)
    set_owner(api, "fish", None)

    logged = entries(api, action="table")
    assert {(e["actor"]["kind"], e["actor"]["user"]["id"]) for e in logged} == {("person", user.id)}
    named = {a["id"]: {"id": a["id"], "name": a["name"]} for a in (scribe, heir, helper)}
    assert [(e["action"], e["target_id"], e["details"]) for e in logged][::-1] == [
        (
            "table.owner_assigned",
            "fish",
            {"table": "fish", "from": None, "to": named[scribe["id"]]},
        ),
        ("table.shared", "fish", {"table": "fish", "agent": named[helper["id"]]}),
        ("table.unshared", "fish", {"table": "fish", "agent": named[helper["id"]]}),
        (
            "table.transferred",
            "fish",
            {"table": "fish", "from": named[scribe["id"]], "to": named[heir["id"]]},
        ),
        ("table.owner_cleared", "fish", {"table": "fish", "from": named[heir["id"]], "to": None}),
    ]


def test_sharing_twice_with_the_owner_or_with_an_archived_agent_is_refused(api, store):
    scribe, helper, gone = writer(api, "Scribe"), writer(api, "Helper"), writer(api, "Gone")
    create_table(api, store, scribe, "notes")
    api.post(f"/api/agents/{gone['id']}/archive")
    share(api, "notes", helper)

    for agent, says in (
        (helper, "already"),
        (scribe, "owns"),
        (gone, "archived"),
    ):
        resp = share(api, "notes", agent)
        assert resp.status_code == 409, resp.text
        assert says in resp.json()["detail"]
    # Nor can an archived agent be made an owner.
    assert set_owner(api, "notes", gone).status_code == 409
    # And a table with no owner is anyone's: there's nothing to share.
    assert share(api, "fish", helper).status_code == 409


def test_a_table_or_agent_there_isnt_is_not_found(api, store):
    agent = writer(api, "Scribe")

    assert set_owner(api, "nope", agent).status_code == 404
    assert share(api, "nope", agent).status_code == 404
    assert api.put("/api/data/tables/fish/owner", json={"agent_id": 99999}).status_code == 404


def test_an_archived_owners_tables_are_read_only_until_transferred(api, store):
    scribe, helper, heir = writer(api, "Scribe"), writer(api, "Helper"), writer(api, "Heir")
    create_table(api, store, scribe, "notes")
    share(api, "notes", helper)

    # The archive dialog lists the tables it leaves read-only.
    used = api.get(f"/api/agents/{scribe['id']}/used-by").json()
    assert used["tables"] == [{"name": "notes"}]
    assert api.post(f"/api/agents/{scribe['id']}/archive").status_code == 200

    assert listed(api)["notes"]["owner"] == who(scribe, archived=True)
    for agent in (helper, heir):
        refused = write(api, agent, "notes", note="no")
        assert "read-only" in refused["error"] and "Scribe" in refused["error"]

    assert set_owner(api, "notes", heir).status_code == 200
    assert write(api, heir, "notes", note="yes")["row_count"] == 1
    assert write(api, helper, "notes", note="shared")["row_count"] == 1


def drop_table(store: DataStore, table: str) -> str:
    spec = SchemaChange.model_validate({"ops": [{"op": "drop_table", "table": table}]})
    request = schema_changes.propose(store.id, spec, "Gone", run_id=None, tool_call_id=None)
    rows = StoreRows(store.id)
    rows.execute(
        update(SchemaChangeRequest)
        .where(SchemaChangeRequest.id == request.id)
        .values(status=SchemaChangeStatus.approved)
    )
    assert schema_changes.apply(store.id, request.id) == SchemaChangeStatus.applied
    return rows.get(SchemaChangeRequest, request.id).ops[0]["dropped_as"]


def ownership_rows(store: DataStore) -> list[tuple]:
    with as_owner(store) as conn:
        owners = conn.execute("SELECT table_name, agent_id FROM aios.table_owners").fetchall()
        shares = conn.execute("SELECT table_name, agent_id FROM aios.table_shares").fetchall()
    return sorted(owners) + sorted(shares)


def test_ownership_follows_a_table_through_drop_and_restore_and_goes_on_purge(
    api, store, monkeypatch
):
    scribe, helper = writer(api, "Scribe"), writer(api, "Helper")
    create_table(api, store, scribe, "notes")
    share(api, "notes", helper)

    dropped_as = drop_table(store, "notes")

    assert ownership_rows(store) == [(dropped_as, scribe["id"]), (dropped_as, helper["id"])]
    assert api.get(f"/api/agents/{scribe['id']}/used-by").json()["tables"] == []
    [item] = dropped.listing(store.id)
    assert dropped.restore(store.id, item[0].id) is None

    assert listed(api)["notes"]["owner"] == who(scribe)
    assert listed(api)["notes"]["shares"] == [who(helper)]
    assert write(api, helper, "notes", note="back")["row_count"] == 1

    drop_table(store, "notes")
    monkeypatch.setattr(dropped.get_settings(), "data_store_dropped_retention_days", 0)
    assert dropped.purge(store.id) == ["Purge dropped table data.notes"]

    assert ownership_rows(store) == []


def test_make_seed_makes_owned_tables(db):
    """`make seed`'s organisation (`aios.seed.build`) has tables made by approved schema
    changes: one its writer owns and shares with its reviewer, and one an archived agent owns."""
    from aios import seed

    seed.build(db)
    made = db.scalar(select(User).where(User.email == seed.EMAIL))
    try:
        with store_session(data_store_for(org_of(db, made).id), "ro") as store:
            found = ownership.access(store, ["notes", "reading_list"])
        notes, reading = found["notes"], found["reading_list"]
        assert (notes.owner.name, [a.name for a in notes.shares]) == ("Writer", ["Reviewer"])
        assert (reading.owner.name, reading.owner.archived) == ("Old researcher", True)
    finally:
        delete_user(db, made)
