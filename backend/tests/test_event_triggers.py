"""Event triggers through the trigger API: what editors and admins make, and what they're
told."""

from conftest import join_organisation
from test_chats import signed_in_as
from test_permissions import set_role
from test_triggers import make_agent, make_trigger, make_workflow

from aios.models import Role


def document_trigger(target_kind: str, target_id: int, **extra) -> dict:
    return {
        "kind": "event",
        "target_kind": target_kind,
        "target_id": target_id,
        "event_type": "document_ingested",
        "input": "Summarise the new document.",
        **extra,
    }


def test_create_a_document_ingested_trigger_with_filters(api):
    agent = make_agent(api, "Reader")
    t = make_trigger(
        api,
        document_trigger(
            "agent",
            agent["id"],
            filter={"content_types": ["application/PDF", " text/* "], "sources": []},
        ),
    )
    assert t["kind"] == "event"
    assert t["event_type"] == "document_ingested"
    # Normalised: lower case, trimmed, and empty fields left out.
    assert t["filter"] == {"content_types": ["application/pdf", "text/*"]}
    assert t["fire_on_own_events"] is False
    assert t["disabled_reason"] is None
    assert (
        t["cron"] is None
        and t["next_run_at"] is None
        and t["description"] == "When a document is ingested"
    )
    assert t["target_name"] == "Reader"
    assert api.get(f"/api/triggers/{t['id']}").json() == t
    assert [x["id"] for x in api.get("/api/triggers").json()] == [t["id"]]


def test_a_workflow_event_trigger_needs_no_template(api):
    workflow = make_workflow(api, "Pipeline")
    t = make_trigger(api, document_trigger("workflow", workflow["id"], input=""))
    assert t["input"] == "" and t["target_kind"] == "workflow"


def test_unknown_filter_fields_are_refused(api):
    agent = make_agent(api, "Reader")
    resp = api.post(
        "/api/triggers", json=document_trigger("agent", agent["id"], filter={"tables": ["x"]})
    )
    assert resp.status_code == 422
    assert "tables" in resp.json()["detail"]
    resp = api.post(
        "/api/triggers", json=document_trigger("agent", agent["id"], filter={"sources": ["x"]})
    )
    assert resp.status_code == 422


def test_an_event_trigger_needs_an_event_type_it_can_fire_on(api):
    agent = make_agent(api, "Reader")
    body = document_trigger("agent", agent["id"])
    del body["event_type"]
    assert api.post("/api/triggers", json=body).status_code == 422
    # Not yet: phase 3's later events.
    body["event_type"] = "table_written"
    assert api.post("/api/triggers", json=body).status_code == 422


def test_editing_turns_a_schedule_into_an_event_trigger(api):
    agent = make_agent(api, "Reader")
    t = make_trigger(
        api,
        {
            "kind": "schedule",
            "target_kind": "agent",
            "target_id": agent["id"],
            "cron": "0 9 * * *",
            "input": "Go",
        },
    )
    resp = api.put(
        f"/api/triggers/{t['id']}",
        json=document_trigger("agent", agent["id"], fire_on_own_events=True),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["kind"] == "event" and out["fire_on_own_events"] is True
    assert out["cron"] is None and out["next_run_at"] is None


def test_only_editors_and_admins_make_or_change_triggers(api, db, user):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    body = document_trigger("agent", agent["id"])
    participant = join_organisation(db, user, "participant")
    set_role(db, participant, Role.participant, of=user)
    with signed_in_as(participant):
        assert api.get("/api/triggers").status_code == 200
        assert api.post("/api/triggers", json=body).status_code == 403
        assert api.put(f"/api/triggers/{t['id']}", json=body).status_code == 403
        assert api.delete(f"/api/triggers/{t['id']}").status_code == 403
    editor = join_organisation(db, user, "editor")
    set_role(db, editor, Role.editor, of=user)
    with signed_in_as(editor):
        assert api.post("/api/triggers", json=body).status_code == 201


def test_an_agents_archive_confirmation_lists_its_event_triggers(api):
    agent = make_agent(api, "Reader")
    t = make_trigger(api, document_trigger("agent", agent["id"]))
    used = api.get(f"/api/agents/{agent['id']}/used-by").json()
    assert used["triggers"] == [
        {
            "id": t["id"],
            "enabled": True,
            "cron": None,
            "description": "When a document is ingested",
            "timezone": None,
            "next_run_at": None,
        }
    ]
