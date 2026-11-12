"""The people mirror (`aios.people` in each store, `aios.datastores.people`): kept by the
platform as people join, leave, are renamed and change role; filled when a store is provisioned
or migrated; read for who made and changed things; rebuilt by the repair sweep. And the deny
list that keeps people's emails from the data tools."""

from datetime import datetime

import psycopg
import pytest
from conftest import (
    delete_user,
    join_organisation,
    org_of,
    signed_in_client,
    store_of,
)
from sqlalchemy import delete, select, update
from test_agents import agent_body, make_agent
from test_chats import signed_in_as
from test_data_tools import DATA_TOOLS, as_owner, describe, query
from test_invitations import leave_behind, make_link
from test_provisioning import store_row

from aios import organisations, worker
from aios.config import get_settings, libpq_conninfo
from aios.datastores import people, provision
from aios.datastores.connections import dispose_engines
from aios.datastores.migrations import head_revision
from aios.models import DataStoreStatus, OrganisationMember, Role, User


def mirror(db, user: User) -> dict[int, tuple]:
    """The user's organisation's mirror, as the store has it, by person id: (first name, last
    name, email, role, left)."""
    store = store_row(db, store_of(user))
    with as_owner(store) as conn:
        rows = conn.execute(
            "SELECT id, first_name, last_name, email, role, left_at IS NOT NULL FROM aios.people"
        ).fetchall()
    return {r[0]: tuple(r[1:]) for r in rows}


def test_a_new_store_is_provisioned_with_its_organisations_people_in_the_mirror(db, user):
    assert mirror(db, user) == {user.id: ("Test", "User", user.email, "admin", False)}
    with as_owner(store_row(db, store_of(user))) as conn:
        (joined,) = conn.execute("SELECT joined_at FROM aios.people").fetchone()
    assert isinstance(joined, datetime)


def test_an_existing_store_gets_the_mirror_from_the_store_migration_command(db, user):
    """A store made before the mirror: at the revision before it, without the table."""
    store_id = store_of(user)
    store = store_row(db, store_id)
    with as_owner(store) as conn:
        # What the revisions after it made, then the mirror.
        conn.execute("DROP SCHEMA insights CASCADE")
        conn.execute("DROP TABLE aios.turn_tool_calls")
        conn.execute("ALTER TABLE aios.documents DROP COLUMN synced_item_id")
        conn.execute("DROP TABLE aios.source_syncs, aios.synced_items CASCADE")
        conn.execute("DROP TYPE aios.sync_kind, aios.sync_status, aios.synced_item_kind")
        conn.execute("ALTER TABLE aios.sources DROP COLUMN next_sync_at")
        conn.execute("DROP TABLE aios.memories CASCADE")
        conn.execute("DROP TABLE aios.events CASCADE")
        conn.execute("ALTER TABLE aios.chat_messages DROP COLUMN verdict")
        conn.execute("DROP TABLE aios.table_shares, aios.table_owners")
        conn.execute("DROP TABLE aios.usage CASCADE")
        conn.execute("DROP TYPE aios.usage_kind, aios.usage_purpose")
        conn.execute(
            "ALTER TABLE aios.documents DROP COLUMN verdict, DROP COLUMN verdict_reason,"
            " DROP COLUMN verdict_version, DROP COLUMN checked_at, DROP COLUMN content_hash,"
            " DROP COLUMN released_by, DROP COLUMN released_at, DROP COLUMN released_content_hash"
        )
        conn.execute("DROP TABLE aios.search_items")
        conn.execute("DROP TYPE aios.search_item_kind, aios.verdict")
        conn.execute("ALTER TABLE aios.chunks ALTER COLUMN embedding SET NOT NULL")
        conn.execute("DROP SCHEMA reporting CASCADE")
        # The audit log refuses truncation, but not being dropped.
        conn.execute("DROP TABLE aios.audit_log, aios.approval_rules")
        conn.execute("DROP FUNCTION aios.audit_log_append_only()")
        conn.execute("DROP TYPE aios.audit_actor_kind")
        conn.execute("DROP TABLE aios.turns")
        conn.execute("DROP TYPE aios.turn_status")
        conn.execute(
            "DROP TABLE aios.schema_change_requests, aios.invocations, aios.chat_messages,"
            " aios.chat_members, aios.chats, aios.triggers CASCADE"
        )
        conn.execute(
            "DROP TYPE aios.schema_change_status, aios.invocation_status, aios.invocation_cause,"
            " aios.chat_message_kind, aios.chat_author_kind,"
            " aios.chat_member_kind, aios.trigger_status,"
            " aios.trigger_target_kind, aios.trigger_kind, aios.event_type"
        )
        conn.execute("ALTER TABLE aios.documents DROP COLUMN source_id")
        conn.execute("DROP TABLE aios.sources")
        conn.execute("DROP TYPE aios.source_kind, aios.source_status")
        conn.execute(
            "DROP TABLE aios.workflow_run_steps, aios.workflow_runs, aios.sandbox_sessions,"
            " aios.agent_runs, aios.workflow_edges, aios.workflow_nodes, aios.workflow_versions,"
            " aios.workflows, aios.agent_version_tools, aios.agent_version_skills,"
            " aios.agent_versions, aios.agents, aios.skill_versions, aios.skills CASCADE"
        )
        conn.execute(
            "DROP TYPE aios.workflow_node_kind, aios.workflow_step_status,"
            " aios.workflow_run_status, aios.invocable_by"
        )
        conn.execute("DROP SCHEMA langgraph CASCADE")
        conn.execute("DROP TABLE aios.people")
        conn.execute("UPDATE aios.alembic_version SET version_num = '0007'")
    db.execute(update(type(store)).where(type(store).id == store_id).values(schema_revision="0007"))
    db.commit()

    outcome = provision.migrate_all([store_id])

    assert outcome == {store_id: head_revision()}
    assert mirror(db, user) == {user.id: ("Test", "User", user.email, "admin", False)}


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation (not its creator), joined through a link."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    leave_behind(db, u)


def test_joining_through_a_link_reaches_the_mirror(api, db, user, new_user):
    link = make_link(api)
    other = new_user()
    try:
        with signed_in_as(other):
            assert api.post(f"/api/invites/{link['token']}/accept").status_code == 200
        assert mirror(db, user)[other.id] == (
            other.first_name,
            "Tester",
            other.email,
            "editor",
            False,
        )
    finally:
        db.execute(
            delete(OrganisationMember).where(
                OrganisationMember.user_id == other.id,
                OrganisationMember.organisation_id == org_of(db, user).id,
            )
        )
        db.commit()


def test_leaving_keeps_the_person_in_the_mirror_as_a_former_member(api, db, user, member):
    people.refresh(org_of(db, user).id)
    assert mirror(db, user)[member.id][-1] is False

    with signed_in_as(member):
        assert api.post("/api/organisation/leave").status_code == 200

    assert mirror(db, user)[member.id] == ("Member", "Person", member.email, "editor", True)


def test_a_rename_and_a_new_email_reach_the_mirror_of_every_organisation_theyre_in(db, user):
    client = signed_in_client(user)
    assert client.patch("/api/me", json={"last_name": "Renamed"}).status_code == 200
    assert mirror(db, user)[user.id][:2] == ("Test", "Renamed")

    body = {
        "email": "renamed-person@example.com",
        "current_password": "correct horse battery staple",
    }
    assert client.put("/api/me/email", json=body).status_code == 200
    assert mirror(db, user)[user.id][2] == "renamed-person@example.com"


def test_a_role_change_reaches_the_mirror(db, user, member):
    """Handing the organisation to another member is a role change for both."""
    org = org_of(db, user)
    people.refresh(org.id)
    organisations.change_role(db, org, member.id, Role.admin)
    organisations.change_role(db, org, user.id, Role.editor)
    db.commit()

    roles = {i: row[3] for i, row in mirror(db, user).items()}
    assert roles == {user.id: "editor", member.id: "admin"}


def test_a_platform_change_that_rolls_back_never_reaches_the_mirror(db, user):
    db.execute(update(User).where(User.id == user.id).values(first_name="Never"))
    people.changed(db, org_of(db, user).id)
    db.rollback()
    db.commit()

    assert mirror(db, user)[user.id][0] == "Test"


def test_a_store_not_ready_yet_is_left_to_its_provisioning(db, new_user):
    u = new_user()
    store_id = store_of(u)
    assert store_row(db, store_id).status == DataStoreStatus.provisioning

    assert people.refresh(org_of(db, u).id) is None

    provision.provision(store_id)
    assert set(mirror(db, u)) == {u.id}


# --- Who made and changed things ------------------------------------------------------------


def test_lists_and_pages_say_who_made_and_changed_things_from_the_mirror(api, db, user, member):
    people.refresh(org_of(db, user).id)
    with signed_in_as(member):
        agent = make_agent(api, "Helper")
    # The mirror is what's read: a name only it has shows.
    with as_owner(store_row(db, store_of(user))) as conn:
        conn.execute("UPDATE aios.people SET first_name = 'Mirrored' WHERE id = %s", (member.id,))

    listed = next(a for a in api.get("/api/agents").json() if a["id"] == agent["id"])
    page = api.get(f"/api/agents/{agent['id']}").json()

    for shown in (listed, page):
        assert shown["created_by"] == {
            "id": member.id,
            "name": "Mirrored Person",
            "former_member": False,
        }


def test_someone_who_has_left_shows_as_a_former_member(api, db, user, member):
    people.refresh(org_of(db, user).id)
    with signed_in_as(member):
        agent = make_agent(api, "Helper")
        assert api.post("/api/organisation/leave").status_code == 200

    page = api.get(f"/api/agents/{agent['id']}").json()

    assert page["created_by"] == {"id": member.id, "name": "Member Person", "former_member": True}
    assert page["updated_by"]["former_member"] is True


def test_someone_the_mirror_hasnt_caught_up_with_is_named_from_the_platform(api, db, user, member):
    """The member joined without the mirror being told (a lost update, until the sweep)."""
    with signed_in_as(member):
        agent = make_agent(api, "Helper")

    page = api.get(f"/api/agents/{agent['id']}").json()

    assert page["created_by"] == {"id": member.id, "name": "Member Person", "former_member": False}


# --- The repair sweep -----------------------------------------------------------------------


def test_the_repair_sweep_rebuilds_deleted_and_stale_rows_and_reports_them(db, user, member):
    store_id = store_of(user)
    people.refresh(org_of(db, user).id)
    with as_owner(store_row(db, store_id)) as conn:
        conn.execute("DELETE FROM aios.people WHERE id = %s", (user.id,))
        conn.execute("UPDATE aios.people SET role = 'admin' WHERE id = %s", (member.id,))
    before = mirror(db, user)

    report = people.repair([store_id])

    assert report == {store_id: people.Fixed(added=[user.id], updated=[member.id], left=[])}
    assert mirror(db, user) == {
        user.id: ("Test", "User", user.email, "admin", False),
        member.id: ("Member", "Person", member.email, "editor", False),
    }
    assert before != mirror(db, user)
    # Nothing left to fix: the next sweep reports nothing.
    assert people.repair([store_id]) == {}


def test_the_repair_sweep_marks_someone_whose_leaving_was_lost_as_left(db, user, member):
    store_id = store_of(user)
    people.refresh(org_of(db, user).id)
    db.execute(delete(OrganisationMember).where(OrganisationMember.user_id == member.id))
    db.commit()

    assert people.repair([store_id]) == {store_id: people.Fixed(left=[member.id])}
    assert mirror(db, user)[member.id][-1] is True


def test_the_repair_sweep_runs_on_the_maintenance_workers_beat(monkeypatch):
    swept = []
    monkeypatch.setattr(people, "repair", lambda: swept.append(True) or {})

    worker.repair_stores_task()

    assert swept == [True]
    assert worker.celery_app.conf.task_routes["aios.repair_stores"] == {"queue": "routine"}
    scheduled = {e["task"] for e in worker.celery_app.conf.beat_schedule.values()}
    assert "aios.repair_stores" in scheduled


# --- The deny list --------------------------------------------------------------------------


@pytest.fixture
def data_agent(api) -> dict:
    agent = make_agent(api, "Analyst")
    return api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=DATA_TOOLS)).json()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM aios.people",
        "SELECT * FROM aios.people",
        "SELECT p FROM aios.people p",
        "SELECT count(email) FROM aios.people",
        "SELECT id FROM aios.people WHERE email LIKE '%@%'",
        "SELECT (SELECT string_agg(email, ',') FROM aios.people)",
    ],
)
def test_query_data_cant_read_peoples_emails(api, db, user, data_agent, sql):
    result = query(api, data_agent, sql)

    assert set(result) == {"error"}
    assert user.email not in str(result)


def test_query_data_reads_the_rest_of_the_mirror(api, db, user, data_agent):
    result = query(api, data_agent, "SELECT first_name, last_name, role FROM aios.people")

    assert result["rows"] == [["Test", "User", "admin"]]


def test_describe_data_lists_the_mirror_without_emails(api, user, data_agent):
    tables = describe(api, data_agent)["tables"]

    mirrored = next(t for t in tables if (t["schema"], t["name"]) == ("aios", "people"))
    names = [c["name"] for c in mirrored["columns"]]
    assert names == ["id", "first_name", "last_name", "role", "joined_at", "left_at", "updated_at"]
    assert "email" not in str(tables)


def test_the_deny_list_holds_in_a_store_migrated_to_the_mirror(api, db, user, data_agent):
    store_id = store_of(user)
    provision.migrate_all([store_id])

    assert set(query(api, data_agent, "SELECT email FROM aios.people")) == {"error"}


def test_dropping_a_store_drops_its_agents_group_role_too(db, new_user):
    store_id = store_of(new_user())
    provision.provision(store_id)
    names = provision.StoreNames.of(store_row(db, store_id))
    dispose_engines()

    provision.drop(names)

    with psycopg.connect(libpq_conninfo(get_settings().data_store_provisioner_url)) as conn:
        for role in (names.group, names.writer):
            found = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            assert found.fetchone() is None


def test_make_seed_fills_the_mirror(db):
    """`make seed`'s organisation (`aios.seed.build`), made as its member would make it."""
    from aios import seed

    seed.build(db)
    made = db.scalar(select(User).where(User.email == seed.EMAIL))
    try:
        assert mirror(db, made) == {made.id: ("Dev", "Tester", seed.EMAIL, "admin", False)}
    finally:
        delete_user(db, made)
