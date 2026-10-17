"""Schema change requests can't wait for ever, and those from runs that are gone don't linger.

- A scheduled task expires requests still pending after `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`.
  That counts as a rejection: the run that asked resumes with "expired", and the model is told.
- Deleting a chat run, a workflow run, or an agent whose run waits cancels its pending request.
  Nothing resumes: the run is gone.
- Either way the request's notifications are cleared, and no change is ever applied."""

from datetime import timedelta

from fake_model import model, use_model
from sqlalchemy import update
from test_schema_change_requests import (  # noqa: F401
    CONTACTS,
    Script,
    agent,
    data_tables,
    queued,
    run_queued,
    start,
    store,
)

from aios import worker
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import SchemaChangeRequest


def asked_ago(request: dict, age: timedelta) -> None:
    """Make the request as old as `age`."""
    with SessionLocal() as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .values(created_at=SchemaChangeRequest.created_at - age)
        )
        db.commit()


def expiry() -> timedelta:
    return timedelta(days=get_settings().schema_change_request_expiry_days)


def expire(queued: list) -> None:  # noqa: F811
    """The scheduled task, as the provisioning worker's beat runs it."""
    worker.expire_schema_change_requests_task.run()


def request_of(api, request: dict) -> dict:
    return api.get(f"/api/schema-changes/{request['id']}").json()


def test_a_request_pending_past_the_expiry_resumes_its_chat_run_with_expired(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    script = Script(CONTACTS)
    run, request = start(api, agent, script)
    asked_ago(request, expiry() + timedelta(minutes=1))

    expire(queued)
    with use_model(model(script)):
        run_queued(queued)

    assert request_of(api, request)["status"] == "expired"
    (_, outcome), _ = script.results
    assert outcome["status"] == "expired"
    assert "expired" in outcome["message"]
    assert "nothing changed" in outcome["message"]
    run = api.get(f"/api/agents/{agent['id']}/runs/{run['id']}").json()
    assert run["waiting"] is False
    assert data_tables(store) == []


def test_only_requests_past_the_expiry_expire_and_their_notifications_clear(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    _, young = start(api, agent, Script(CONTACTS))
    _, old = start(api, agent, Script({**CONTACTS, "name": "places"}))
    asked_ago(young, expiry() - timedelta(minutes=1))
    asked_ago(old, expiry() + timedelta(minutes=1))

    expire(queued)

    assert request_of(api, young)["status"] == "pending"
    expired = request_of(api, old)
    assert expired["status"] == "expired"
    assert expired["decided_at"] is not None
    listed = api.get("/api/notifications").json()
    assert listed["unread"] == 1
    unread = {n["ref"]["request_id"] for n in listed["notifications"] if n["read_at"] is None}
    assert unread == {young["id"]}


def test_deleting_a_waiting_chat_run_cancels_its_request_and_nothing_resumes(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    run, request = start(api, agent, Script(CONTACTS))

    r = api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}")

    assert r.status_code == 204
    cancelled = request_of(api, request)
    assert cancelled["status"] == "cancelled"
    assert cancelled["decided_at"] is not None
    assert api.get("/api/notifications").json()["unread"] == 0
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 409
    expire(queued)
    assert queued == []
    assert data_tables(store) == []


def test_deleting_an_agent_whose_run_waits_cancels_its_request(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    _, request = start(api, agent, Script(CONTACTS))

    assert api.delete(f"/api/agents/{agent['id']}").status_code == 204

    assert request_of(api, request)["status"] == "cancelled"
    assert api.get("/api/notifications").json()["unread"] == 0
    assert queued == []
    assert data_tables(store) == []


def test_the_expiry_period_is_configuration(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    _, request = start(api, agent, Script(CONTACTS))
    asked_ago(request, timedelta(hours=2))
    monkeypatch.setattr(get_settings(), "schema_change_request_expiry_days", 1 / 24)

    expire(queued)

    assert request_of(api, request)["status"] == "expired"
