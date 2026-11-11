"""The data store is the organisation's: every member reaches the one store (its documents,
tables, usage, schema change requests, dropped objects and run history export), each document
records who uploaded it, and a user in another organisation reaches none of it."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F401, F811

import asyncio
import contextlib

import pytest
from conftest import build_user, join_organisation, provisioned_user, store_of
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from test_agents import chat_body, make_agent
from test_data_tools import as_owner
from test_dropped import apply, listed, propose, restores, run_restores, store
from test_history_export import set_up_and_export, stored
from test_organisations import before_the_contract, revision
from test_schema_change_requests import queued
from test_workflows import agent_node, make_workflow

from aios import organisations
from aios.api import documents
from aios.api import workflows as workflows_api
from aios.auth import current_user
from aios.main import app
from aios.models import (
    DataStore,
    OrganisationMember,
    SchemaChangeRequest,
    SchemaChangeStatus,
    User,
)
from aios.testing.fake_model import replies, use_model
from aios.workflows import execute_run


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    db.delete(u)
    db.commit()


@pytest.fixture
def outsider(db):
    """A user in another organisation, with its own store provisioned."""
    from conftest import delete_user

    u = provisioned_user(db, "outsider")
    yield u
    delete_user(db, u)


@contextlib.contextmanager
def signed_in_as(user: User):
    """Make the `api` client's requests come from `user` within the block."""
    previous = app.dependency_overrides[current_user]
    app.dependency_overrides[current_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides[current_user] = previous


@pytest.fixture
def ingests(monkeypatch) -> list[tuple]:
    """The uploads queued for ingest, instead of sent to the broker."""
    calls: list[tuple] = []
    monkeypatch.setattr(documents.ingest_document_task, "delay", lambda *a: calls.append(a))
    return calls


def upload(api, name: str) -> dict:
    r = api.post("/api/documents", files={"file": (name, b"# Hi", "text/markdown")})
    assert r.status_code == 201, r.text
    return r.json()


def uploaders(api) -> list[tuple[str, str | None]]:
    """Each listed document's filename and its uploader's name, newest first."""
    r = api.get("/api/documents")
    assert r.status_code == 200, r.text
    return [(d["filename"], (d["uploaded_by"] or {}).get("name")) for d in r.json()]


# --- The upgrade ---------------------------------------------------------------------------------


def test_the_upgrade_points_each_store_at_its_owners_organisation_with_their_export_flags(db):
    """The data step (revision 0035), on stores made before they were organisations': each one
    owned by a user in their personal organisation (revision 0030), one with the export and
    embedding on."""
    step = revision("0035_organisation_data_stores")
    exporting = build_user("exporting")
    quiet = build_user("quiet")
    db.add_all([exporting, quiet])
    db.flush()
    for u in (exporting, quiet):
        organisations.create_personal(db, u)
    on = DataStore(
        owner_organisation_id=organisations.membership_of(db, exporting.id).id,
        export_run_history=True,
        embed_run_history=True,
    )
    off = DataStore(owner_organisation_id=organisations.membership_of(db, quiet.id).id)
    stores = {exporting.id: on, quiet.id: off}
    db.add_all(stores.values())
    db.flush()
    # As before the upgrade, a store is its user's, with their flags, and has no organisation
    # (which the check below forbids), in this test's transaction only: the contract's
    # downgrade gives it back its user and their flags, and the store is then undone.
    before_the_contract(db)
    db.execute(text(f"ALTER TABLE data_stores DROP CONSTRAINT {step.CHECK}"))
    db.execute(
        update(DataStore)
        .where(DataStore.id.in_([s.id for s in stores.values()]))
        .values(owner_organisation_id=None, export_run_history=False, embed_run_history=False)
    )

    step.backfill(db.connection())

    for u, flags in ((exporting, (True, True)), (quiet, (False, False))):
        row = stores[u.id]
        db.refresh(row)
        assert row.owner_organisation_id == organisations.membership_of(db, u.id).id
        assert (row.export_run_history, row.embed_run_history) == flags

    # Run again, it changes nothing.
    step.backfill(db.connection())
    db.refresh(stores[quiet.id])
    assert stores[quiet.id].owner_organisation_id == organisations.membership_of(db, quiet.id).id


def test_a_store_has_an_organisation_unless_its_being_deleted(db, user):
    """Its organisation is gone (`SET NULL`) only once the store is going."""
    row = db.get(DataStore, store_of(user))
    row.owner_organisation_id = None
    with pytest.raises(IntegrityError, match="ck_data_stores_owner_organisation"):
        db.flush()
    db.rollback()


# --- Documents, tables and usage ----------------------------------------------------------------


def test_members_share_the_organisations_store_and_see_who_uploaded_each_document(
    api, user, member, ingests
):
    assert store_of(member) == store_of(user)
    mine = upload(api, "mine.md")
    assert mine["uploaded_by"] == {"id": user.id, "name": "Test User"}

    with signed_in_as(member):
        theirs = upload(api, "theirs.md")
        assert theirs["uploaded_by"]["name"] == "Member Person"
        assert uploaders(api) == [("theirs.md", "Member Person"), ("mine.md", "Test User")]
        # Another member's document is theirs to download and delete too.
        download = api.get(f"/api/documents/{mine['id']}/download", follow_redirects=False)
        assert download.status_code == 302
        member_view = (api.get("/api/data-store").json(), api.get("/api/data/tables").json())

    assert uploaders(api) == [("theirs.md", "Member Person"), ("mine.md", "Test User")]
    assert (api.get("/api/data-store").json(), api.get("/api/data/tables").json()) == member_view
    assert api.delete(f"/api/documents/{theirs['id']}").status_code == 204
    with signed_in_as(member):
        assert uploaders(api) == [("mine.md", "Test User")]


def test_a_document_from_before_uploads_were_recorded_shows_no_uploader(api, user):
    from conftest import add_document

    add_document(store_of(user), filename="old.md")

    assert uploaders(api) == [("old.md", None)]


def test_another_organisation_sees_none_of_the_stores_documents_or_tables(
    db, api, user, outsider, ingests
):
    mine = upload(api, "mine.md")
    with as_owner(db.get(DataStore, store_of(user))) as conn:
        conn.execute("CREATE TABLE data.fish (name text)")

    with signed_in_as(outsider):
        assert uploaders(api) == []
        assert api.get("/api/data/tables").json()["tables"] == []
        assert api.get(f"/api/documents/{mine['id']}/download").status_code == 404
        assert api.delete(f"/api/documents/{mine['id']}").status_code == 404

    assert uploaders(api) == [("mine.md", "Test User")]


# --- Schema change requests and dropped objects -------------------------------------------------


def notified(api, request: SchemaChangeRequest) -> bool:
    """Whether the signed-in user has an unread notification of the request."""
    listed = api.get("/api/notifications").json()["notifications"]
    return any(n["ref"]["request_id"] == str(request.id) and not n["read_at"] for n in listed)


def test_a_schema_change_request_notifies_every_member_and_any_member_decides_it(
    db,
    api,
    user,
    member,
    store,
    queued,
):
    drop = propose(store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    assert notified(api, drop)

    with signed_in_as(member):
        assert notified(api, drop)
        # First in the Data page's Changes, pending, for them to decide.
        listed_request = api.get("/api/schema-changes").json()[0]
        assert (listed_request["id"], listed_request["can_decide"]) == (str(drop.id), True)
        impact = api.get(f"/api/schema-changes/{drop.id}/impact")
        assert impact.status_code == 200
        assert [r["column"] for r in impact.json()["removes"]] == ["weight_kg"]
        approved = api.post(f"/api/schema-changes/{drop.id}/approve", json={"confirm": "fish"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        # Decided, it's read for everyone it notified.
        assert not notified(api, drop)
    assert not notified(api, drop)
    assert db.get(SchemaChangeRequest, drop.id).decided_by_user_id == member.id

    add = propose(
        store, {"op": "add_column", "table": "fish", "column": {"name": "colour", "type": "text"}}
    )
    with signed_in_as(member):
        rejected = api.post(f"/api/schema-changes/{add.id}/reject", json={"note": "Not now"})
        assert rejected.status_code == 200, rejected.text
    db.expire_all()
    assert db.get(SchemaChangeRequest, add.id).status == SchemaChangeStatus.rejected


def test_any_member_restores_a_dropped_table(
    db,
    api,
    user,
    member,
    store,
    restores,
):
    apply(db, store, {"op": "drop_table", "table": "fish"})

    with signed_in_as(member):
        [table] = listed(api)
        assert api.post(f"/api/dropped/{table['id']}/restore").status_code == 202
    run_restores(restores)

    assert listed(api) == []
    assert [t["name"] for t in api.get("/api/data/tables").json()["tables"]] == ["fish"]


def test_another_organisation_cant_see_or_decide_the_stores_requests_or_restore_its_drops(
    db,
    api,
    user,
    outsider,
    store,
    queued,
    restores,
):
    request = propose(store, {"op": "drop_column", "table": "fish", "column": "weight_kg"})
    apply(db, store, {"op": "drop_table", "table": "fish"})
    [table] = listed(api)

    with signed_in_as(outsider):
        assert api.get("/api/notifications").json()["notifications"] == []
        assert api.get("/api/schema-changes").json() == []
        assert api.get(f"/api/schema-changes/{request.id}").status_code == 404
        assert api.get(f"/api/schema-changes/{request.id}/impact").status_code == 404
        for decision in ("approve", "reject"):
            assert (
                api.post(f"/api/schema-changes/{request.id}/{decision}", json={}).status_code == 404
            )
        assert listed(api) == []
        assert api.post(f"/api/dropped/{table['id']}/restore").status_code == 404
    assert (queued, restores) == ([], [])


# --- The run history export ---------------------------------------------------------------------


def test_the_export_is_the_organisations_and_takes_every_members_runs(
    db, api, user, member, history_tasks, monkeypatch
):
    started: list[int] = []
    monkeypatch.setattr(workflows_api.run_workflow_task, "delay", started.append)
    store_id = store_of(user)
    mine = make_agent(api, "Mine")
    my_run = api.post(f"/api/agents/{mine['id']}/runs").json()
    with use_model(replies("hi")):
        url = f"/api/agents/{mine['id']}/runs/{my_run['id']}/chat"
        assert api.post(url, json=chat_body("from me")).status_code == 200
    with signed_in_as(member):
        theirs = make_agent(api, "Theirs")
        workflow = make_workflow(api, "flow", [agent_node("one", theirs)])
        flow_run = api.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "go"}).json()
    with use_model(replies("went")):
        asyncio.run(execute_run(flow_run["id"]))

    # Switched on by the member, it's on for the organisation.
    with signed_in_as(member):
        r = api.put("/api/organisation/settings", json={"export_run_history": True})
        assert r.status_code == 200 and r.json()["export_run_history"] is True
    assert api.get("/api/organisation/settings").json()["export_run_history"] is True
    db.expire_all()
    assert db.get(DataStore, store_id).export_run_history is True

    assert history_tasks["set_up"] == [store_id]
    set_up_and_export(store_id, history_tasks)
    assert stored(store_id, "SELECT agent_name, title FROM history.agent_runs ORDER BY id") == [
        ("Mine", "from me"),
        ("Theirs", "go"),
    ]
    assert stored(store_id, "SELECT workflow_name, status FROM history.workflow_runs") == [
        ("flow", "succeeded")
    ]

    # And it's the organisation's to switch off, for every member.
    api.put("/api/organisation/settings", json={"export_run_history": False})
    with signed_in_as(member):
        assert api.get("/api/organisation/settings").json()["export_run_history"] is False


def test_another_organisations_export_is_its_own(db, api, user, outsider, history_tasks):
    r = api.put("/api/organisation/settings", json={"export_run_history": True})
    assert r.status_code == 200

    with signed_in_as(outsider):
        assert api.get("/api/organisation/settings").json()["export_run_history"] is False
    assert (
        db.scalar(select(DataStore.export_run_history).where(DataStore.id == store_of(outsider)))
        is False
    )
    assert history_tasks["set_up"] == [store_of(user)]


def test_the_user_settings_endpoint_is_gone(api):
    """The export moved to the organisation's settings; the user has none of their own yet."""
    assert api.get("/api/settings").status_code == 404
