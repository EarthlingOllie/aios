"""Approval rules (`aios.approval_rules`): who may decide each kind of schema change. Editors
create tables, add columns and create indexes; admins decide drops, restores, purges and
ownership changes; a request needs the highest role among its operations. Admins may raise a
rule but never lower it below its default, and both the change and each decision are audited."""

import pytest
from conftest import join_organisation, store_of
from sqlalchemy import select
from test_audit_log import entries
from test_chats import signed_in_as
from test_data_tools import as_owner, provisioned
from test_permissions import set_role

from aios import worker
from aios.datastores import schema_changes
from aios.datastores.schema_changes import SchemaChange
from aios.models import DataStore, Notification, Role

ADD_NOTE = {"op": "add_column", "table": "fish", "column": {"name": "note", "type": "text"}}
DROP_WEIGHT = {"op": "drop_column", "table": "fish", "column": "weight_kg"}
CONTACTS = {
    "op": "create_table",
    "name": "contacts",
    "columns": [{"name": "name", "type": "text", "nullable": False}],
}


@pytest.fixture
def store(db, user) -> DataStore:
    """The signed-in admin's store, with a `data.fish` table."""
    row = provisioned(db, store_of(user))
    with as_owner(row) as conn:
        conn.execute("CREATE TABLE data.fish (id int PRIMARY KEY, weight_kg numeric)")
    return row


@pytest.fixture
def editor(db, user):
    return join_organisation(db, user, "editor")


@pytest.fixture(autouse=True)
def queued(monkeypatch) -> list:
    """What deciding queues, instead of sent to the broker."""
    tasks: list = []
    monkeypatch.setattr(worker.apply_schema_change_task, "delay", lambda *a: tasks.append(a))
    monkeypatch.setattr(worker, "enqueue_resume", lambda *a: tasks.append(a))
    return tasks


def propose(store: DataStore, *ops: dict):
    spec = SchemaChange.model_validate({"ops": list(ops)})
    return schema_changes.propose(store.id, spec, "Tidy up", run_id=None, tool_call_id=None)


def notified(db, request) -> set[int]:
    db.expire_all()
    return set(
        db.scalars(
            select(Notification.user_id).where(
                Notification.kind == "approval_needed",
                Notification.ref["approval_id"].astext == str(request.id),
            )
        )
    )


def rules(api) -> dict[str, dict]:
    resp = api.get("/api/approval-rules")
    assert resp.status_code == 200, resp.text
    return {r["kind"]: r for r in resp.json()}


def test_the_default_rules(api, store):
    assert {k: (r["role"], r["default_role"]) for k, r in rules(api).items()} == {
        "create_table": ("editor", "editor"),
        "add_column": ("editor", "editor"),
        "create_index": ("editor", "editor"),
        "drop_table": ("admin", "admin"),
        "drop_column": ("admin", "admin"),
        "restore": ("admin", "admin"),
        "purge": ("admin", "admin"),
        "change_ownership": ("admin", "admin"),
    }


def test_an_editor_cant_approve_a_request_mixing_an_added_and_a_dropped_column(
    api, db, user, store, editor
):
    request = propose(store, ADD_NOTE, DROP_WEIGHT)

    assert notified(db, request) == {user.id}
    with signed_in_as(editor):
        seen = api.get(f"/api/schema-changes/{request.id}").json()
        refused = api.post(f"/api/schema-changes/{request.id}/approve", json={"confirm": "fish"})
        rejected = api.post(f"/api/schema-changes/{request.id}/reject", json={})
    assert (seen["needs_role"], seen["can_decide"]) == ("admin", False)
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"]
    assert rejected.status_code == 403
    assert api.get(f"/api/schema-changes/{request.id}").json()["status"] == "pending"


def test_an_editor_approves_adding_a_column_and_editors_are_notified(api, db, user, store, editor):
    request = propose(store, ADD_NOTE, CONTACTS)

    assert notified(db, request) == {user.id, editor.id}
    with signed_in_as(editor):
        seen = api.get(f"/api/schema-changes/{request.id}").json()
        approved = api.post(f"/api/schema-changes/{request.id}/approve", json={})
    assert (seen["needs_role"], seen["can_decide"]) == ("editor", True)
    assert approved.status_code == 200, approved.text


def test_participants_are_never_notified(api, db, user, store, editor):
    participant = join_organisation(db, user, "participant")
    set_role(db, participant, Role.participant, of=user)

    request = propose(store, CONTACTS)

    assert participant.id not in notified(db, request)


def test_lowering_a_rule_below_its_default_is_refused(api, store):
    resp = api.put("/api/approval-rules/drop_column", json={"role": "editor"})

    assert resp.status_code == 422
    assert rules(api)["drop_column"]["role"] == "admin"


def test_raising_a_rule_works_and_is_audited(api, db, user, store, editor):
    resp = api.put("/api/approval-rules/add_column", json={"role": "admin"})

    assert resp.status_code == 200, resp.text
    assert rules(api)["add_column"]["role"] == "admin"
    (entry,) = entries(api, action="approval_rule.changed")
    assert entry["actor"]["user"]["id"] == user.id
    assert (entry["target_kind"], entry["target_id"]) == ("approval_rule", "add_column")
    assert entry["details"] == {"from": "editor", "to": "admin"}

    request = propose(store, ADD_NOTE)
    assert notified(db, request) == {user.id}
    with signed_in_as(editor):
        assert api.post(f"/api/schema-changes/{request.id}/approve", json={}).status_code == 403

    # And back down to its default.
    assert api.put("/api/approval-rules/add_column", json={"role": "editor"}).status_code == 200
    assert rules(api)["add_column"]["role"] == "editor"


def test_only_admins_change_rules(api, store, editor):
    with signed_in_as(editor):
        assert rules(api)["add_column"]["role"] == "editor"
        refused = api.put("/api/approval-rules/add_column", json={"role": "admin"})
    assert refused.status_code == 403


def test_an_unknown_kind_is_404(api, store):
    assert api.put("/api/approval-rules/teleport", json={"role": "admin"}).status_code == 404


def test_decisions_are_audited(api, db, user, store):
    approved = propose(store, CONTACTS)
    rejected = propose(store, ADD_NOTE)

    api.post(f"/api/schema-changes/{approved.id}/approve", json={})
    api.post(f"/api/schema-changes/{rejected.id}/reject", json={"note": "Not now"})

    decided = {e["target_id"]: e for e in entries(api, action="approval.decided")}
    assert decided[str(approved.id)]["details"]["decision"] == "approved"
    assert decided[str(rejected.id)]["details"] == {
        "decision": "rejected",
        "summary": rejected.summary,
        "note": "Not now",
        "needs_role": "editor",
    }
    assert decided[str(approved.id)]["actor"]["user"]["id"] == user.id


def test_the_kinds_of_a_request(store):
    from aios import approval_rules

    assert approval_rules.kinds_of([ADD_NOTE, CONTACTS]) == {"add_column", "create_table"}
    assert (
        approval_rules.needed(
            {"add_column": Role.editor, "drop_column": Role.admin},
            [
                ADD_NOTE,
                DROP_WEIGHT,
            ],
        )
        == Role.admin
    )
