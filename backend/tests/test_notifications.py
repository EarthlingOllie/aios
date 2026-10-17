"""In-app notifications: a pending schema change request notifies whoever can decide it, and
deciding it clears them. Each user only ever sees their own."""

from test_schema_change_requests import CONTACTS, Script, agent, queued, start, store  # noqa: F401


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
