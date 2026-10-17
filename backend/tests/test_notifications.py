"""In-app notifications: a pending schema change request notifies whoever can decide it, and
deciding it clears them. Each user only ever sees their own."""

import uuid

import pytest
from test_schema_change_requests import CONTACTS, Script, agent, queued, start, store  # noqa: F401

from aios import worker
from aios.auth import current_user
from aios.main import app


def test_marking_one_read_is_idempotent(api, agent, queued):  # noqa: F811
    start(api, agent, Script(CONTACTS))
    [notification] = api.get("/api/notifications").json()["notifications"]

    first = api.post(f"/api/notifications/{notification['id']}/read")
    again = api.post(f"/api/notifications/{notification['id']}/read")

    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json()["read_at"] is not None
    # Read once: the second call changes nothing.
    assert again.json() == first.json()
    assert api.get("/api/notifications").json()["unread"] == 0


def test_unread_come_first_newest_first(api, agent, queued):  # noqa: F811
    _, first = start(api, agent, Script(CONTACTS))
    _, second = start(api, agent, Script({**CONTACTS, "name": "places"}))
    _, third = start(api, agent, Script({**CONTACTS, "name": "things"}))
    api.post(f"/api/schema-changes/{third['id']}/reject", json={})

    listed = api.get("/api/notifications").json()

    assert listed["unread"] == 2
    assert [n["ref"]["request_id"] for n in listed["notifications"]] == [
        second["id"],
        first["id"],
        third["id"],
    ]


def test_users_only_see_and_mark_their_own(api, agent, queued, new_user):  # noqa: F811
    start(api, agent, Script(CONTACTS))
    [mine] = api.get("/api/notifications").json()["notifications"]
    other = new_user()
    app.dependency_overrides[current_user] = lambda: other

    assert api.get("/api/notifications").json() == {"unread": 0, "notifications": []}
    assert api.post(f"/api/notifications/{mine['id']}/read").status_code == 404
    assert api.post(f"/api/notifications/{uuid.uuid4()}/read").status_code == 404


def test_requesting_a_change_notifies_the_owner(api, agent, store, queued):  # noqa: F811
    _, request = start(api, agent, Script(CONTACTS))

    listed = api.get("/api/notifications").json()

    assert listed["unread"] == 1
    [notification] = listed["notifications"]
    assert notification["kind"] == "schema_change_request"
    assert notification["ref"] == {
        "request_id": request["id"],
        "data_store_id": str(store.id),
    }
    assert notification["read_at"] is None


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_deciding_the_request_marks_its_notification_read(api, agent, queued, decision):  # noqa: F811
    _, request = start(api, agent, Script(CONTACTS))

    assert api.post(f"/api/schema-changes/{request['id']}/{decision}", json={}).status_code == 200

    listed = api.get("/api/notifications").json()
    assert listed["unread"] == 0
    [notification] = listed["notifications"]
    assert notification["read_at"] is not None


def test_a_decision_that_cant_be_queued_leaves_the_notification_unread(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    """The decision is undone (503), so the request still waits for one."""
    _, request = start(api, agent, Script(CONTACTS))

    def broker_down(*args):
        raise ConnectionError("Redis is down")

    monkeypatch.setattr(worker.apply_schema_change_task, "delay", broker_down)
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 503

    assert api.get("/api/notifications").json()["unread"] == 1
