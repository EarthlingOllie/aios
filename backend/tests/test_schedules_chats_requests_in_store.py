"""Schedules, chats (with their members, messages and invocations) and schema change requests
are in each organisation's store. The platform holds no organisation content, only a thin index
of when each schedule is next due (`aios.schedules`), which saving a schedule writes and the
repair sweep rebuilds; and notifications, which name a request by its id and store."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from conftest import (
    delete_user,
    org_of,
    provisioned_user,
    signed_in_store,
    store_of,
    user_store,
)
from sqlalchemy import delete, select, text, update
from test_chats import make_chat, post, signed_in_as
from test_schema_change_requests import CONTACTS
from test_triggers import make_agent, make_trigger, schedule

from aios import scheduler, schedules, worker
from aios.cron import next_run
from aios.datastores import schema_changes, store_session
from aios.datastores.models import AgentRun, Trigger
from aios.datastores.schema_changes import SchemaChange
from aios.db import SessionLocal
from aios.models import OrganisationMember, Role, ScheduleDue
from aios.testing.fake_model import replies, use_model

# What the platform database keeps: identity, organisations, memberships, invitations, the store
# registry (with its credentials), notifications, the schedule due index and the work queue.
# Everything an organisation makes is in its store.
PLATFORM_TABLES = {
    "alembic_version",
    "users",
    "organisations",
    "organisation_members",
    "organisation_invites",
    "organisation_deletions",
    "data_stores",
    "notifications",
    "schedule_due",
    # Where each webhook trigger's token leads (`aios.webhooks`): token hashes and store ids.
    "webhook_tokens",
    # Platform-side audit entries on their way to their store's audit log (`aios.audit`).
    "audit_outbox",
    # The run service's work queue (`aios.run_service`): store ids, deleted once done.
    "work_items",
    # Agents' login roles in their stores (`aios.datastores.agent_roles`): names and encrypted
    # passwords, credentials like the stores' own.
    "agent_store_roles",
    # Connectors' credentials (`aios.connectors`): encrypted, kept out of every store on purpose.
    "connector_credentials",
    # What each store used when measured (`aios.datastores.quota`): sizes, for Insights.
    "store_usage_samples",
}
MOVED = (
    "triggers",
    "chats",
    "chat_members",
    "chat_messages",
    "invocations",
    "schema_change_requests",
)


def test_the_platform_holds_no_organisation_content_and_the_store_holds_what_moved(db, user):
    tables = set(db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")))
    assert tables == PLATFORM_TABLES
    with user_store(user) as store:
        for table in MOVED:
            assert store.scalar(text("SELECT to_regclass(:t)"), {"t": f"aios.{table}"}), table


def due_rows(store_id) -> dict[int, datetime]:
    """The store's rows of the due index: when each schedule is next due, by trigger id."""
    with SessionLocal() as db:
        return dict(
            db.execute(
                select(ScheduleDue.trigger_id, ScheduleDue.next_run_at).where(
                    ScheduleDue.data_store_id == store_id
                )
            ).all()
        )


def next_at(store_id, trigger_id: int) -> datetime | None:
    with store_session(store_id) as store:
        return store.scalar(select(Trigger.next_run_at).where(Trigger.id == trigger_id))


def test_saving_a_schedule_writes_its_due_index_row(api):
    store_id = signed_in_store()
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"], cron="0 9 * * *", timezone="UTC")
    t = make_trigger(api, body)
    assert due_rows(store_id) == {t["id"]: next_at(store_id, t["id"])}
    assert next_at(store_id, t["id"]) == next_run("0 9 * * *", "UTC", datetime.now(UTC))

    # Rescheduled: the row moves with it.
    resp = api.put(f"/api/triggers/{t['id']}", json={**body, "cron": "30 18 * * *"})
    assert resp.status_code == 200, resp.text
    assert due_rows(store_id) == {t["id"]: next_at(store_id, t["id"])}
    assert next_at(store_id, t["id"]).hour == 18

    # Paused: nothing is due.
    resp = api.put(f"/api/triggers/{t['id']}", json={**body, "enabled": False})
    assert resp.status_code == 200, resp.text
    assert due_rows(store_id) == {}
    # Resumed, then paused with its agent's archive.
    api.put(f"/api/triggers/{t['id']}", json=body)
    assert set(due_rows(store_id)) == {t["id"]}
    assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
    assert due_rows(store_id) == {}
    assert api.post(f"/api/agents/{agent['id']}/unarchive").status_code == 200

    # Deleted: gone from the index too.
    other = make_trigger(api, body)
    assert set(due_rows(store_id)) == {other["id"]}
    assert api.delete(f"/api/triggers/{other['id']}").status_code == 204
    assert due_rows(store_id) == {}


def test_the_repair_sweep_rebuilds_lost_stale_and_orphan_rows_and_reports_them(api, caplog):
    store_id = signed_in_store()
    agent = make_agent(api, "Bot")
    lost = make_trigger(api, schedule("agent", agent["id"]))
    stale = make_trigger(api, schedule("agent", agent["id"]))
    paused = make_trigger(api, schedule("agent", agent["id"], enabled=False))
    with SessionLocal() as db:
        # A write of the index that never landed, one that landed out of date, and a row left
        # for a schedule that's since been paused.
        db.execute(delete(ScheduleDue).where(ScheduleDue.trigger_id == lost["id"]))
        db.execute(
            update(ScheduleDue)
            .where(ScheduleDue.trigger_id == stale["id"])
            .values(next_run_at=datetime(2001, 1, 1, tzinfo=UTC))
        )
        db.commit()
    schedules.index(store_id, [(paused["id"], datetime(2001, 1, 1, tzinfo=UTC))])

    with caplog.at_level(logging.WARNING, logger="aios.schedules"):
        report = schedules.repair([store_id])

    fixed = report[store_id]
    assert (fixed.added, fixed.updated, fixed.removed) == (
        [lost["id"]],
        [stale["id"]],
        [paused["id"]],
    )
    assert "Repaired data store" in caplog.text and str(store_id) in caplog.text
    assert due_rows(store_id) == {
        lost["id"]: next_at(store_id, lost["id"]),
        stale["id"]: next_at(store_id, stale["id"]),
    }
    # Nothing left to fix.
    assert schedules.repair([store_id]) == {}


def test_the_repair_sweep_task_rebuilds_the_due_index(monkeypatch):
    swept = []
    monkeypatch.setattr(schedules, "repair", lambda: swept.append("schedules"))
    monkeypatch.setattr("aios.datastores.people.repair", lambda: swept.append("people"))
    worker.repair_stores_task.run()
    assert swept == ["people", "schedules"]


def test_a_due_row_ahead_of_its_schedule_only_moves(api):
    """The index is a copy: the trigger in the store decides. A row that says it's due when
    the trigger isn't (a write lost, say) fires nothing, and is put right."""
    store_id = signed_in_store()
    agent = make_agent(api, "Bot")
    t = make_trigger(api, schedule("agent", agent["id"]))
    now = datetime.now(UTC)
    schedules.index(store_id, [(t["id"], now - timedelta(minutes=1))])

    with SessionLocal() as db:
        assert scheduler.claim_due(db, now) == []
    assert due_rows(store_id) == {t["id"]: next_at(store_id, t["id"])}


def test_an_edit_racing_a_claim_drops_that_firing(api):
    store_id = signed_in_store()
    agent = make_agent(api, "Bot")
    body = schedule("agent", agent["id"])
    t = make_trigger(api, body)
    now = datetime.now(UTC)
    with store_session(store_id) as store:
        store.execute(
            update(Trigger)
            .where(Trigger.id == t["id"])
            .values(next_run_at=now - timedelta(seconds=1))
        )
        store.commit()
    schedules.index(store_id, [(t["id"], now - timedelta(seconds=1))])
    with SessionLocal() as db:
        assert scheduler.claim_due(db, now) == [(store_id, t["id"])]

    # Edited after the claim, before the firing starts its run: the firing was the old
    # schedule's, and is dropped when it re-reads the trigger under its lock.
    resp = api.put(f"/api/triggers/{t['id']}", json={**body, "input": "Something else"})
    assert resp.status_code == 200, resp.text
    with use_model(replies("hi")):
        asyncio.run(scheduler.fire(store_id, t["id"], now))

    with store_session(store_id) as store:
        assert store.scalars(select(AgentRun)).all() == []
    shown = api.get(f"/api/triggers/{t['id']}").json()
    assert (shown["last_fired_at"], shown["last_status"], shown["last_run_id"]) == (None,) * 3


def test_schedules_with_the_same_id_in_two_stores_each_fire_in_their_own(api, db, user):
    """Ids are each store's own: the index, and what's claimed, name the store too."""
    other = provisioned_user(db, "other")
    try:
        now = datetime.now(UTC)
        made = {}
        for owner in (user, other):
            with signed_in_as(owner):
                agent = make_agent(api, "Bot")
                made[store_of(owner)] = make_trigger(api, schedule("agent", agent["id"]))
        assert {t["id"] for t in made.values()} == {1}
        for store_id, t in made.items():
            with store_session(store_id) as store:
                store.execute(
                    update(Trigger)
                    .where(Trigger.id == t["id"])
                    .values(next_run_at=now - timedelta(seconds=1))
                )
                store.commit()
            schedules.index(store_id, [(t["id"], now - timedelta(seconds=1))])

        with SessionLocal() as s:
            claimed = scheduler.claim_due(s, now)
        assert sorted(claimed) == sorted((store_id, 1) for store_id in made)
        for store_id in made:
            assert next_at(store_id, 1) > now
            assert due_rows(store_id) == {1: next_at(store_id, 1)}
    finally:
        delete_user(db, other)


def test_a_chats_messages_members_and_unread_counts_are_in_the_store(api, db, user):
    chat = make_chat(api, "Design")
    post(api, chat, "Hello")
    with user_store(user) as store:
        assert store.scalar(text("SELECT count(*) FROM aios.chats")) == 1
        assert store.scalar(text("SELECT count(*) FROM aios.chat_messages")) == 1
        # The member and the documents assistant.
        assert store.scalar(text("SELECT count(*) FROM aios.chat_members")) == 2
    [listed] = api.get("/api/chats").json()
    assert (listed["name"], listed["unread_count"]) == ("Design", 0)


def test_a_notification_opens_the_right_organisations_approval(api, db, user):
    """A person in two organisations is notified of a request in each one's store; each
    notification names its organisation, and its request is that organisation's alone."""
    other = provisioned_user(db, "other")
    try:
        theirs = org_of(db, other)
        db.add(OrganisationMember(organisation_id=theirs.id, user_id=user.id, role=Role.editor))
        db.commit()
        mine = org_of(db, user)
        made = {}
        for org, store_id in ((mine, store_of(user)), (theirs, store_of(other))):
            made[org.id] = schema_changes.propose(
                store_id,
                SchemaChange.model_validate({"ops": [{**CONTACTS, "name": f"t{org.id}"}]}),
                "Because",
                run_id=None,
                tool_call_id=None,
            )
        listed = api.get("/api/me/notifications").json()["notifications"]
        by_request = {n["ref"]["approval_id"]: n for n in listed}
        for org_id, request in made.items():
            n = by_request[str(request.id)]
            assert n["organisation"]["id"] == org_id
            assert n["detail"] == request.summary
            # Opened in its organisation, the approval is there; in the other, it isn't.
            here = api.get(f"/api/o/{org_id}/approvals/{request.id}")
            assert here.status_code == 200, here.text
            assert here.json()["can_decide"] is True
            elsewhere = next(o for o in made if o != org_id)
            assert api.get(f"/api/o/{elsewhere}/approvals/{request.id}").status_code == 404
    finally:
        delete_user(db, other)
