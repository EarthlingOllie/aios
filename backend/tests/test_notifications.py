"""In-app notifications: a pending schema change request notifies whoever can decide it, and
deciding it clears them. Each user only ever sees their own."""

import pytest
from test_schema_change_requests import CONTACTS, Script, agent, queued, start, store  # noqa: F401

from aios import worker


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
