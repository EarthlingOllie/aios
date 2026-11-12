"""The audit log (`aios.audit`): an append-only table in each store that the application can't
alter, written by store-side actions in their own transaction and by platform-side actions
through the platform's outbox; read by admins on the audit log endpoint, and by agents through
the reporting view, without its details."""

from datetime import UTC, datetime

import psycopg
import pytest
from conftest import join_organisation, org_of, store_of, user_store
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_agents import agent_body, make_agent, make_skill
from test_chats import signed_in_as
from test_data_tools import provisioned, run_tools
from test_invitations import make_link
from test_triggers import make_trigger, make_workflow, schedule

from aios import audit, datastores
from aios.datastores import DataStoreBusy, provision
from aios.db import SessionLocal
from aios.models import AuditOutbox, DataStore
from aios.testing.fake_model import ToolCall
from aios.tools import write_data


def record_one(user, action: str = "agent.archived") -> int:
    with user_store(user) as store:
        audit.record(store, audit.person(user.id), action, "agent", 1, {"name": "Scout"})
        store.commit()
        return store.scalar(text("SELECT max(id) FROM aios.audit_log"))


def names_of(user) -> provision.StoreNames:
    with SessionLocal() as db:
        return provision.StoreNames.of(db.get_one(DataStore, store_of(user)))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE aios.audit_log SET action = 'nothing.happened' WHERE id = :id",
        "DELETE FROM aios.audit_log WHERE id = :id",
        "TRUNCATE aios.audit_log",
    ],
)
def test_the_app_cant_update_or_delete_audit_entries(user, statement):
    entry = record_one(user)

    with user_store(user) as store, pytest.raises(DBAPIError):
        store.execute(text(statement), {"id": entry})

    with user_store(user) as store:
        assert (
            store.scalar(text("SELECT action FROM aios.audit_log WHERE id = :id"), {"id": entry})
            == "agent.archived"
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE aios.audit_log SET action = 'nothing.happened' WHERE id = %(id)s",
        "DELETE FROM aios.audit_log WHERE id = %(id)s",
        "TRUNCATE aios.audit_log",
    ],
)
def test_not_even_the_stores_owner_can(user, statement):
    entry = record_one(user)

    with provision.as_owner(names_of(user)) as conn, pytest.raises(psycopg.Error):
        conn.execute(statement, {"id": entry})

    with user_store(user) as store:
        assert (
            store.scalar(text("SELECT action FROM aios.audit_log WHERE id = :id"), {"id": entry})
            == "agent.archived"
        )


# --- Platform-side actions, through the outbox ------------------------------------------------


def logged(user, action: str) -> list[dict]:
    with user_store(user) as store:
        rows = store.execute(
            text(
                "SELECT actor_kind::text, actor_user_id, target_kind, target_id, details,"
                " outbox_id FROM aios.audit_log WHERE action = :action ORDER BY id"
            ),
            {"action": action},
        ).all()
        return [r._asdict() for r in rows]


def outbox_of(db, user) -> list[AuditOutbox]:
    db.expire_all()
    return db.scalars(
        select(AuditOutbox).where(AuditOutbox.organisation_id == org_of(db, user).id)
    ).all()


def test_a_role_change_reaches_the_log_through_the_outbox(api, db, user):
    member = join_organisation(db, user, "member")

    changed = api.put(f"/api/organisation/members/{member.id}/role", json={"role": "participant"})

    assert changed.status_code == 200
    assert [r.action for r in outbox_of(db, user)] == ["member.role_changed"]
    audit.drain()
    assert outbox_of(db, user) == []
    (entry,) = logged(user, "member.role_changed")
    assert entry["actor_kind"] == "person"
    assert entry["actor_user_id"] == user.id
    assert (entry["target_kind"], entry["target_id"]) == ("member", str(member.id))
    assert entry["details"] == {"from": "editor", "to": "participant"}


def test_an_entry_written_while_the_store_is_down_arrives_once_its_back(api, db, user, monkeypatch):
    member = join_organisation(db, user, "member")
    api.put(f"/api/organisation/members/{member.id}/role", json={"role": "admin"})

    def down(*args, **kwargs):
        raise DataStoreBusy("the store is down")

    with monkeypatch.context() as m:
        m.setattr(datastores, "store_session", down)
        audit.drain()
    assert len(outbox_of(db, user)) == 1
    assert logged(user, "member.role_changed") == []

    audit.drain()
    audit.drain()

    assert outbox_of(db, user) == []
    assert len(logged(user, "member.role_changed")) == 1


def test_delivering_an_outbox_row_again_records_it_once(api, db, user):
    """The store committed, but the outbox row wasn't deleted (the platform went away): the
    next drain delivers it again, and the store keeps the first."""
    member = join_organisation(db, user, "member")
    api.put(f"/api/organisation/members/{member.id}/role", json={"role": "admin"})
    (row,) = outbox_of(db, user)
    kept = {c.key: getattr(row, c.key) for c in AuditOutbox.__table__.columns}
    db.expunge(row)

    audit.drain()
    db.add(AuditOutbox(**kept))
    db.commit()
    audit.drain()

    (entry,) = logged(user, "member.role_changed")
    assert entry["outbox_id"] == kept["id"]
    assert outbox_of(db, user) == []


def test_the_routine_workers_beat_drains_the_outbox_every_ten_seconds(monkeypatch):
    from aios import worker

    drained = []
    monkeypatch.setattr(audit, "drain", lambda: drained.append(True) or 0)

    worker.drain_audit_outbox_task()

    assert drained == [True]
    assert worker.celery_app.conf.task_routes["aios.drain_audit_outbox"] == {"queue": "routine"}
    (entry,) = [
        e
        for e in worker.celery_app.conf.beat_schedule.values()
        if e["task"] == "aios.drain_audit_outbox"
    ]
    assert entry["schedule"] == 10


def test_renames_invitations_joining_leaving_and_deletion_are_audited(api, db, user, new_user):
    org = org_of(db, user)
    api.put("/api/organisation", json={"name": "Renamed Org"})
    link = make_link(api, role="participant")
    joiner = new_user()
    with signed_in_as(joiner):
        assert api.post(f"/api/invites/{link['token']}/accept").status_code == 200
    revoked = make_link(api)
    api.delete(f"/api/organisation/invites/{revoked['id']}")
    with signed_in_as(joiner):
        left = api.post(f"/api/o/{org.id}/organisation/leave")
    assert left.status_code == 200, left.text
    api.post("/api/organisation/deletion", json={"name": "Renamed Org"})
    api.delete("/api/organisation/deletion")

    rows = outbox_of(db, user)
    assert [(r.action, r.actor_user_id) for r in rows] == [
        ("organisation.renamed", user.id),
        ("invitation.created", user.id),
        ("member.joined", joiner.id),
        ("invitation.created", user.id),
        ("invitation.revoked", user.id),
        ("member.left", joiner.id),
        ("organisation.deletion_scheduled", user.id),
        ("organisation.deletion_cancelled", user.id),
    ]
    assert rows[0].details == {"from": user.full_name, "to": "Renamed Org"}
    assert rows[2].details["role"] == "participant"


# --- Store-side actions ----------------------------------------------------------------------


def actions_of(user) -> list[tuple[str, str | None, str | None]]:
    with user_store(user) as store:
        return [
            tuple(r)
            for r in store.execute(
                text("SELECT action, target_kind, target_id FROM aios.audit_log ORDER BY id")
            )
        ]


def test_configuration_changes_and_archiving_are_audited_with_what_they_did(api, user):
    agent = make_agent(api, "Scout")
    api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, base_prompt="Look around"))
    api.post(f"/api/agents/{agent['id']}/archive")
    api.post(f"/api/agents/{agent['id']}/archive")  # already archived: nothing happens
    api.post(f"/api/agents/{agent['id']}/unarchive")
    skill = make_skill(api, "Haiku")
    api.post(f"/api/skills/{skill['id']}/archive")
    workflow = make_workflow(api, "Pipeline")
    trigger = make_trigger(api, schedule("workflow", workflow["id"]))
    api.delete(f"/api/triggers/{trigger['id']}")

    a, s, w, t = str(agent["id"]), str(skill["id"]), str(workflow["id"]), str(trigger["id"])
    assert actions_of(user) == [
        ("agent.created", "agent", a),
        ("agent.updated", "agent", a),
        ("agent.archived", "agent", a),
        ("agent.unarchived", "agent", a),
        ("skill.created", "skill", s),
        ("skill.archived", "skill", s),
        ("workflow.created", "workflow", w),
        ("schedule.created", "schedule", t),
        ("schedule.deleted", "schedule", t),
    ]
    entry = entries(api, action="agent.updated")[0]
    assert entry["actor"]["kind"] == "person"
    assert (entry["actor"]["user"]["id"], entry["actor"]["user"]["name"]) == (user.id, "Test User")
    assert entry["details"] == {"name": "Scout", "version": 2}


# --- Reading it ------------------------------------------------------------------------------


def entries(api, **filters) -> list[dict]:
    resp = api.get("/api/audit-log", params=filters)
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def test_admins_read_the_log_newest_first_filtered_by_actor_action_and_date(api, db, user):
    member = join_organisation(db, user, "member")
    with user_store(user) as store:
        for day, actor, action in [
            (1, user.id, "agent.created"),
            (2, member.id, "agent.archived"),
            (3, user.id, "skill.created"),
            (4, member.id, "agent.unarchived"),
        ]:
            at = datetime(2026, 1, day, 10, tzinfo=UTC)
            audit.record(store, audit.person(actor), action, "agent", day, at=at)
        store.commit()

    def actions(**filters) -> list[str]:
        return [e["action"] for e in entries(api, **filters)]

    assert actions() == ["agent.unarchived", "skill.created", "agent.archived", "agent.created"]
    assert actions(actor=member.id) == ["agent.unarchived", "agent.archived"]
    assert actions(action="agent") == ["agent.unarchived", "agent.archived", "agent.created"]
    assert actions(action="agent.archived") == ["agent.archived"]
    assert actions(since="2026-01-02", until="2026-01-03") == ["skill.created", "agent.archived"]
    assert actions(actor=user.id, action="skill", until="2026-01-03") == ["skill.created"]


def test_the_log_is_cursor_paged(api, user):
    with user_store(user) as store:
        for day in range(1, 6):
            at = datetime(2026, 2, day, tzinfo=UTC)
            audit.record(store, audit.person(user.id), "agent.created", "agent", day, at=at)
        store.commit()

    first = api.get("/api/audit-log", params={"limit": 2}).json()
    second = api.get("/api/audit-log", params={"limit": 2, "before": first["next"]}).json()
    third = api.get("/api/audit-log", params={"limit": 2, "before": second["next"]}).json()

    pages = [[e["target_id"] for e in page["entries"]] for page in (first, second, third)]
    assert pages == [["5", "4"], ["3", "2"], ["1"]]
    assert third["next"] is None


def test_only_admins_read_the_log(api, db, user):
    editor = join_organisation(db, user, "editor")
    with signed_in_as(editor):
        refused = api.get("/api/audit-log")
    assert refused.status_code == 403
    assert "audit log" in refused.json()["detail"]


# --- What agents read and write: adversarial -------------------------------------------------

DATA_TOOLS = [
    {"key": "describe_data", "config": {}},
    {"key": "query_data", "config": {}},
    {"key": "write_data", "config": {}},
]
SECRET = "the-secret-in-the-details"


@pytest.fixture
def data_agent(api, db, user) -> dict:
    """An agent with the data tools, in a store whose audit log has an entry with details."""
    provisioned(db, store_of(user))
    with user_store(user) as store:
        audit.record(store, audit.person(user.id), "agent.updated", "agent", 7, {"note": SECRET})
        store.commit()
    agent = make_agent(api, "Auditor")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=DATA_TOOLS)).json()


def tool(api, agent, name: str, **args) -> dict:
    return run_tools(api, agent, ToolCall(name, args))[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT details FROM aios.audit_log",
        "SELECT * FROM aios.audit_log",
        "SELECT a FROM aios.audit_log a",
        "SELECT details->>'note' FROM aios.audit_log",
        "SELECT id FROM aios.audit_log WHERE details::text LIKE '%secret%'",
        "SELECT (SELECT string_agg(details::text, ',') FROM aios.audit_log)",
        "SELECT to_jsonb(a) FROM aios.audit_log a",
    ],
)
def test_query_data_cant_read_audit_details(api, data_agent, sql):
    result = tool(api, data_agent, "query_data", sql=sql)

    assert set(result) == {"error"}
    assert SECRET not in str(result)


def test_query_data_reads_the_rest_of_the_audit_log(api, user, data_agent):
    result = tool(
        api,
        data_agent,
        "query_data",
        sql="SELECT action, actor_user_id, target_kind, target_id FROM aios.audit_log"
        " WHERE action = 'agent.updated' AND target_id = '7'",
    )

    assert result["rows"] == [["agent.updated", user.id, "agent", "7"]]


def test_the_reporting_view_shows_only_action_actor_target_and_time(api, user, data_agent):
    result = tool(
        api,
        data_agent,
        "query_data",
        sql="SELECT * FROM reporting.audit_log WHERE target_id = '7'",
    )

    assert result["columns"] == [
        "at",
        "actor_kind",
        "actor_user_id",
        "actor_agent_id",
        "actor_run_id",
        "action",
        "target_kind",
        "target_id",
    ]
    assert len(result["rows"]) == 1
    assert SECRET not in str(result)


def test_describe_data_lists_the_log_without_details_and_the_reporting_view(api, data_agent):
    tables = {(t["schema"], t["name"]): t for t in tool(api, data_agent, "describe_data")["tables"]}

    logged = [c["name"] for c in tables[("aios", "audit_log")]["columns"]]
    assert "details" not in logged
    assert "action" in logged
    reported = [c["name"] for c in tables[("reporting", "audit_log")]["columns"]]
    assert "details" not in reported
    assert "action" in reported


@pytest.mark.parametrize("table", ["aios.audit_log", "reporting.audit_log", "audit_log"])
@pytest.mark.parametrize(
    "args",
    [
        {"operation": "insert", "rows": [{"actor_kind": "system", "action": "forged.entry"}]},
        {"operation": "update", "values": {"action": "forged.entry"}, "where": {"target_id": "7"}},
        {"operation": "delete", "where": {"target_id": "7"}},
    ],
)
def test_write_data_cant_insert_update_or_delete_audit_entries(api, user, data_agent, table, args):
    before = actions_of(user)

    result = tool(api, data_agent, "write_data", table=table, **args)

    assert set(result) == {"error"}
    assert [a for a in actions_of(user) if a[0] != "data.written"] == [
        a for a in before if a[0] != "data.written"
    ]
    assert "forged.entry" not in str(actions_of(user))


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO aios.audit_log (actor_kind, action) VALUES ('system', 'forged.entry')",
        "UPDATE aios.audit_log SET action = 'forged.entry'",
        "DELETE FROM aios.audit_log",
        "SELECT details FROM aios.audit_log",
        "INSERT INTO reporting.audit_log (actor_kind, action) VALUES ('system', 'forged.entry')",
        "DELETE FROM reporting.audit_log",
    ],
)
def test_the_role_write_data_writes_as_cant_touch_the_audit_log(user, data_agent, statement):
    with user_store(user) as session:
        pg = session.connection().connection.driver_connection
        write_data.as_writer(pg)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute(statement)


def test_each_write_data_call_is_audited_with_its_table_operation_rows_and_run(
    api, db, user, data_agent
):
    with provision.as_owner(names_of(user)) as conn:
        conn.execute("CREATE TABLE data.fish (id int PRIMARY KEY, name text)")

    result = tool(
        api,
        data_agent,
        "write_data",
        table="fish",
        operation="insert",
        rows=[{"id": 1, "name": "pike"}, {"id": 2, "name": "perch"}],
    )

    assert result["row_count"] == 2
    (entry,) = entries(api, action="data.written")
    assert entry["actor"]["kind"] == "agent"
    assert entry["actor"]["agent"] == {"id": data_agent["id"], "name": "Auditor"}
    assert entry["actor"]["run_id"] is not None
    assert (entry["target_kind"], entry["target_id"]) == ("table", "data.fish")
    assert entry["details"] == {"table": "data.fish", "operation": "insert", "row_count": 2}


def test_a_refused_write_data_call_records_nothing(api, user, data_agent):
    tool(api, data_agent, "write_data", table="nowhere", operation="insert", rows=[{"id": 1}])

    assert entries(api, action="data.written") == []
