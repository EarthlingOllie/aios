"""Schema change requests can't wait for ever, and those from runs that are gone don't linger.

- A scheduled task expires requests still pending after `SCHEMA_CHANGE_REQUEST_EXPIRY_DAYS`.
  That counts as a rejection: the run that asked resumes with "expired", and the model is told.
- Deleting a chat run, a workflow run, or an agent whose run waits cancels its pending request.
  Nothing resumes: the run is gone.
- Either way the request's notifications are cleared, and no change is ever applied."""

import threading
from datetime import timedelta

import pytest
from conftest import signed_in_store
from sqlalchemy import select, update
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
from aios.datastores import schema_changes, store_session
from aios.datastores.models import AgentRun, SchemaChangeRequest
from aios.models import SchemaChangeStatus
from aios.testing.fake_model import model, use_model


def asked_ago(request: dict, age: timedelta) -> None:
    """Make the request as old as `age`."""
    with store_session(signed_in_store()) as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .values(created_at=SchemaChangeRequest.created_at - age)
        )
        db.commit()


def expiry() -> timedelta:
    return timedelta(days=get_settings().schema_change_request_expiry_days)


def expire(queued: list) -> None:  # noqa: F811
    """The scheduled task, as the routine worker's beat runs it."""
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
    listed = api.get("/api/me/notifications").json()
    assert listed["unread"] == 1
    unread = {n["ref"]["approval_id"] for n in listed["notifications"] if n["read_at"] is None}
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
    assert api.get("/api/me/notifications").json()["unread"] == 0
    assert api.post(f"/api/schema-changes/{request['id']}/approve").status_code == 409
    expire(queued)
    assert queued == []
    assert data_tables(store) == []


def test_an_approval_made_as_it_expires_wins_and_is_applied_once(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    """The approval holds the request's row while it decides; expiry waits, then leaves it."""
    script = Script(CONTACTS)
    _, request = start(api, agent, script)
    asked_ago(request, expiry() + timedelta(minutes=1))
    expired: list = []
    with store_session(signed_in_store()) as deciding:
        # As `_decide` does: lock it, and decide it, but not yet committed.
        row = deciding.scalar(
            select(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .with_for_update()
        )
        row.status = SchemaChangeStatus.approved
        deciding.flush()
        expiring = threading.Thread(target=lambda: expired.extend(schema_changes.expire()))
        expiring.start()
        expiring.join(1)
        assert expiring.is_alive()  # waiting for the decision's lock
        deciding.commit()
    expiring.join(10)

    assert expired == []
    assert request_of(api, request)["status"] == "approved"
    worker.apply_schema_change_task.run(str(store.id), request["id"])
    with use_model(model(script)):
        run_queued(queued)
    assert request_of(api, request)["status"] == "applied"
    assert data_tables(store) == ["contacts"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_expired_request_cant_be_decided(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
    decision,
):
    _, request = start(api, agent, Script(CONTACTS))
    asked_ago(request, expiry() + timedelta(minutes=1))
    expire(queued)
    queued.clear()

    r = api.post(f"/api/schema-changes/{request['id']}/{decision}", json={})

    assert r.status_code == 409
    assert "expired" in r.json()["detail"]
    assert queued == []
    assert data_tables(store) == []


def test_a_cancel_made_as_its_approved_leaves_the_approval(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
):
    run, request = start(api, agent, Script(CONTACTS))
    cancelled: list = []
    with store_session(signed_in_store()) as deciding:
        row = deciding.scalar(
            select(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .with_for_update()
        )
        row.status = SchemaChangeStatus.approved
        deciding.flush()

        def cancel() -> None:
            with store_session(signed_in_store()) as db:
                cancelled.extend(
                    schema_changes.cancel(db, SchemaChangeRequest.requested_by_run == run["id"])
                )
                db.commit()

        cancelling = threading.Thread(target=cancel)
        cancelling.start()
        cancelling.join(1)
        assert cancelling.is_alive()
        deciding.commit()
    cancelling.join(10)

    assert cancelled == []
    assert request_of(api, request)["status"] == "approved"


def test_an_expired_request_nothing_can_resume_isnt_queued_again_and_again(
    api,
    agent,  # noqa: F811
    queued,  # noqa: F811
    monkeypatch,
):
    """E.g. one left pending by a chat run deleted before deleting cancelled requests."""
    _, request = start(api, agent, Script(CONTACTS))
    asked_ago(request, expiry() + timedelta(minutes=1))
    with store_session(signed_in_store()) as db:
        db.execute(
            update(SchemaChangeRequest)
            .where(SchemaChangeRequest.id == request["id"])
            .values(requested_by_run=None)
        )
        db.commit()
    looked_at: list = []
    monkeypatch.setattr(
        worker, "enqueue_resume", lambda store_id, request_id: looked_at.append(request_id)
    )

    expire(queued)
    expire(queued)

    expired = request_of(api, request)
    assert expired["status"] == "expired"
    assert expired["resumed_at"] is not None
    assert str(request["id"]) not in {str(i) for i in looked_at}


def test_a_request_made_as_its_chat_run_is_deleted_is_still_cancelled(
    api,
    agent,  # noqa: F811
    store,  # noqa: F811
    queued,  # noqa: F811
):
    """The turn's request is recorded while the delete runs: the delete waits for it."""
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    deleted: list = []
    with store_session(store.id) as asking:
        # As `schema_changes.propose` records it, holding its run's key share, not yet
        # committed.
        asking.execute(
            select(AgentRun.id)
            .where(AgentRun.id == run["id"])
            .with_for_update(read=True, key_share=True)
        )
        asking.add(
            SchemaChangeRequest(
                ops=[CONTACTS],
                sql="SELECT 1",
                summary="Create table data.contacts",
                reason="For people",
                requested_by_run=run["id"],
                tool_call_id="call_1",
                base_revision=0,
            )
        )
        asking.flush()
        deleting = threading.Thread(
            target=lambda: deleted.append(
                api.delete(f"/api/agents/{agent['id']}/runs/{run['id']}").status_code
            )
        )
        deleting.start()
        deleting.join(1)
        asking.commit()
    deleting.join(10)

    assert deleted == [204]
    [request] = api.get("/api/schema-changes").json()
    assert request["status"] == "cancelled"


def test_the_routine_workers_beat_expires_requests_every_so_often():
    [entry] = [
        e
        for e in worker.celery_app.conf.beat_schedule.values()
        if e["task"] == "aios.expire_schema_change_requests"
    ]
    assert entry["schedule"] == get_settings().schema_change_expiry_check_seconds == 3600
    assert worker.celery_app.conf.task_routes["aios.expire_schema_change_requests"] == {
        "queue": "routine"
    }


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
