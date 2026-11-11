"""Accepting an invitation from an empty personal organisation, which deletes it with its data
store, and the provisioner's sweep that then drops the store (`aios.organisations`,
`aios.datastores.provision`).

The stores here are provisioned for real, as the provisioning tests' are, and what's left of
them is dropped afterwards.
"""

import io
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
import pytest
from conftest import (
    add_document,
    delete_user,
    join_organisation,
    make_user,
    provisioned_user,
    store_of,
)
from sqlalchemy import delete, select, update
from test_agents import make_agent, make_skill
from test_chats import make_chat, make_workflow, signed_in_as
from test_data_tools import as_owner
from test_invitations import make_link
from test_provisioning import login_url, store_row
from test_triggers import make_trigger, schedule

from aios import organisations, worker
from aios.config import get_settings, libpq_conninfo
from aios.datastores import files, provision, store_engine
from aios.db import SessionLocal
from aios.models import (
    Agent,
    DataStore,
    DataStoreStatus,
    Organisation,
    OrganisationMember,
    User,
)


@pytest.fixture
def joiner(db):
    """A user in a personal organisation of their own, with its store provisioned: someone who
    signed up without an invitation. Afterwards they, and whatever is left of their first
    organisation and its store, are deleted."""
    u = provisioned_user(db, "joiner")
    with deleted_after(db, u):
        yield u


@pytest.fixture
def newcomer(db, provisioning):
    """A user who has just signed up without an invitation: in a personal organisation of their
    own, its store made but its provisioning only queued (`provisioning`), not run. Deleted
    afterwards as `joiner` is."""
    u = make_user(db, "newcomer")
    with deleted_after(db, u):
        yield u


@contextmanager
def deleted_after(db, u: User) -> Iterator[None]:
    """Afterwards, delete the user, and whatever is left of their first organisation and its
    store."""
    store_id = store_of(u)
    names = provision.StoreNames.of(store_row(db, store_id))
    yield
    db.rollback()
    db.expire_all()
    org = organisations.membership_of(db, u.id)
    if org is not None and org.created_by == u.id:
        delete_user(db, u)  # still in it: deleted with its store, as any user's
    else:
        db.execute(delete(DataStore).where(DataStore.id == store_id))
        db.execute(delete(User).where(User.id == u.id))
        db.commit()
    provision.drop(names)


def accept(api, link: dict):
    return api.post(f"/api/invites/{link['token']}/accept")


def catalog() -> psycopg.Connection:
    """The provisioner on the platform database, to look in the instance's catalog."""
    url = get_settings().data_store_provisioner_url
    return psycopg.connect(libpq_conninfo(url), autocommit=True)


def database_exists(names: provision.StoreNames) -> bool:
    with catalog() as admin:
        found = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (names.dbname,))
        return bool(found.fetchone())


def roles_left(names: provision.StoreNames) -> list[str]:
    with catalog() as admin:
        rows = admin.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            ([names.owner, names.rw, names.ro],),
        )
        return [r[0] for r in rows]


def files_under(store_id: uuid.UUID) -> list[str]:
    listed = files.client().list_objects_v2(
        Bucket=get_settings().data_store_files_bucket, Prefix=f"stores/{store_id}/"
    )
    return [o["Key"] for o in listed.get("Contents", [])]


def sweep(store_id: uuid.UUID) -> dict:
    """The provisioner's sweep of stores being deleted, on this one only (others in the
    database are left alone)."""
    return provision.delete_deleting([store_id])


def test_accepting_from_an_empty_personal_organisation_deletes_it_and_its_store(
    api, db, user, joiner, store_deletions
):
    link = make_link(api)
    store_id = store_of(joiner)
    names = provision.StoreNames.of(store_row(db, store_id))
    old = organisations.membership_of(db, joiner.id).id
    with signed_in_as(joiner):
        # Things made and put away again: what's left is the defaults, and archived rows. The
        # documents assistant a new chat made, and an archived agent pinning an archived skill's
        # version (which one cascading delete of the organisation would trip over).
        chat = make_chat(api, "scratch")
        assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
        skill = make_skill(api, "Notes")
        agent = make_agent(api, "Pinner", skills=[skill])
        assert api.post(f"/api/agents/{agent['id']}/archive").status_code == 200
        assert api.post(f"/api/skills/{skill['id']}/archive").status_code == 200

        resp = accept(api, link)
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Test User"
        assert api.get("/api/me").json()["organisation"]["name"] == "Test User"

    db.expire_all()
    assert db.get(Organisation, old) is None
    assert db.scalars(
        select(OrganisationMember.organisation_id).where(OrganisationMember.user_id == joiner.id)
    ).all() == [organisations.membership_of(db, user.id).id]
    row = store_row(db, store_id)
    assert (row.status, row.owner_organisation_id) == (DataStoreStatus.deleting, None)
    # The sweep is queued, to run once the accept is committed.
    assert store_deletions == [store_id]
    assert database_exists(names)

    assert sweep(store_id) == {store_id: True}

    assert store_row(db, store_id).status == DataStoreStatus.deleted
    assert not database_exists(names)
    assert roles_left(names) == []


def test_the_sweep_drops_a_deleting_stores_database_roles_and_files(db, joiner):
    store_id = store_of(joiner)
    add_document(store_id)
    files.put(f"stores/{store_id}/elsewhere/left-over", io.BytesIO(b"x"), None)
    row = store_row(db, store_id)
    names = provision.StoreNames.of(row)
    # This process's engine on it, with a connection pooled.
    engine = store_engine(store_id, "rw")
    engine.connect().close()
    assert engine.pool.checkedin() == 1
    # Another store's files are left alone.
    neighbour = uuid.uuid4()
    files.put(f"stores/{neighbour}/documents/1", io.BytesIO(b"y"), None)
    try:
        delete_org(db, joiner)

        assert sweep(store_id) == {store_id: True}

        assert store_row(db, store_id).status == DataStoreStatus.deleted
        assert engine.pool.checkedin() == 0
        assert not database_exists(names)
        assert roles_left(names) == []
        assert files_under(store_id) == []
        assert files_under(neighbour) == [f"stores/{neighbour}/documents/1"]
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(login_url(row, "rw")).close()
        # Done: the next sweep has nothing to do.
        assert sweep(store_id) == {}
    finally:
        files.delete(f"stores/{neighbour}/documents/1")


def delete_org(db, u: User) -> None:
    """Delete the user's organisation as accepting does, leaving its store `deleting`."""
    org = organisations.membership_of(db, u.id)
    organisations.delete_empty(db, org, u)
    db.commit()


@pytest.mark.parametrize("crash_after", range(len(provision.DELETE_STEPS) - 1))
def test_a_sweep_interrupted_midway_is_finished_by_the_next(db, joiner, monkeypatch, crash_after):
    store_id = store_of(joiner)
    add_document(store_id)
    names = provision.StoreNames.of(store_row(db, store_id))
    delete_org(db, joiner)
    steps = list(provision.DELETE_STEPS)

    class Crash(Exception):
        pass

    def crash(deletion):
        steps[crash_after](deletion)
        raise Crash

    monkeypatch.setattr(provision, "DELETE_STEPS", [*steps[:crash_after], crash])
    (outcome,) = sweep(store_id).values()
    assert isinstance(outcome, Crash)
    assert store_row(db, store_id).status == DataStoreStatus.deleting

    monkeypatch.setattr(provision, "DELETE_STEPS", steps)
    assert sweep(store_id) == {store_id: True}

    assert store_row(db, store_id).status == DataStoreStatus.deleted
    assert not database_exists(names)
    assert roles_left(names) == []
    assert files_under(store_id) == []


def test_the_sweep_runs_on_the_provisioning_workers_schedule(db, joiner, monkeypatch):
    store_id = store_of(joiner)
    delete_org(db, joiner)
    swept = provision.delete_deleting
    monkeypatch.setattr(provision, "delete_deleting", lambda: swept([store_id]))

    worker.delete_data_stores_task.apply().get()

    assert store_row(db, store_id).status == DataStoreStatus.deleted
    scheduled = [e["task"] for e in worker.celery_app.conf.beat_schedule.values()]
    assert "aios.delete_data_stores" in scheduled
    assert worker.celery_app.conf.task_routes["aios.delete_data_stores"] == {
        "queue": "provisioning"
    }


def test_the_sweep_queued_for_a_store_waits_for_a_run_still_on_it(db, joiner):
    store_id = store_of(joiner)
    delete_org(db, joiner)

    # Its provisioning (say) is still on it: the queued sweep waits for it, then deletes it.
    with ThreadPoolExecutor(1) as pool:
        with provision.provisioner_locked(store_id):
            swept = pool.submit(worker.delete_data_stores_task.apply, args=[str(store_id)])
            time.sleep(0.5)
            assert not swept.done()
        swept.result(timeout=30).get()

    assert store_row(db, store_id).status == DataStoreStatus.deleted


def test_accepting_while_the_store_is_being_provisioned_leaves_it_to_the_sweep(
    api, db, user, newcomer, store_deletions, monkeypatch
):
    # Signing up, then accepting an invitation straight away: the accept lands while the new
    # store's provisioning is on its last step.
    link = make_link(api)
    store_id = store_of(newcomer)
    names = provision.StoreNames.of(store_row(db, store_id))
    steps = list(provision.STEPS)

    def accept_meanwhile(run):
        with signed_in_as(newcomer):
            resp = accept(api, link)
        assert resp.status_code == 200, resp.text

    monkeypatch.setattr(provision, "STEPS", [*steps[:-1], accept_meanwhile, steps[-1]])
    assert provision.provision(store_id) is None

    # Left for the sweep, not made ready with no organisation.
    row = store_row(db, store_id)
    assert (row.status, row.owner_organisation_id) == (DataStoreStatus.deleting, None)
    assert store_deletions == [store_id]
    assert sweep(store_id) == {store_id: True}
    assert store_row(db, store_id).status == DataStoreStatus.deleted
    assert not database_exists(names)
    assert roles_left(names) == []


def test_a_store_still_provisioning_is_deleted_without_ever_being_ready(db, new_user):
    u = new_user()
    store_id = store_of(u)
    delete_org(db, u)

    # Its provisioning, queued before, finds it being deleted and leaves it.
    assert provision.provision(store_id) is None
    assert sweep(store_id) == {store_id: True}
    assert store_row(db, store_id).status == DataStoreStatus.deleted
    db.execute(delete(DataStore).where(DataStore.id == store_id))
    db.commit()


def refusal(api, link: dict) -> dict:
    resp = accept(api, link)
    assert resp.status_code == 409, resp.text
    return resp.json()["detail"]


def test_accepting_while_the_organisation_has_things_in_it_says_what_by_kind(api, db, user, joiner):
    link = make_link(api)
    store_id = store_of(joiner)
    old = organisations.membership_of(db, joiner.id).id
    with signed_in_as(joiner):
        agent = make_agent(api, "Helper")
        make_agent(api, "Other helper")
        make_workflow(api, "Flow")
        make_skill(api, "Notes")
        make_chat(api, "general")
        make_trigger(api, schedule("agent", agent["id"]))
        add_document(store_id)
        with as_owner(store_row(db, store_id)) as conn:
            conn.execute("CREATE TABLE data.fish (name text)")

        detail = refusal(api, link)
        assert detail["contents"] == [
            {"kind": "agent", "count": 2},
            {"kind": "workflow", "count": 1},
            {"kind": "skill", "count": 1},
            {"kind": "chat", "count": 1},
            {"kind": "schedule", "count": 1},
            {"kind": "document", "count": 1},
            {"kind": "table", "count": 1},
        ]
        assert detail["message"] == (
            "You're a member of joiner Tester, which has things in it: 2 agents, 1 workflow, "
            "1 skill, 1 chat, 1 schedule, 1 document and 1 data table. Accepting would delete "
            "joiner Tester, so delete them first."
        )
        assert api.get("/api/me").json()["organisation"]["name"] == "joiner Tester"
    db.expire_all()
    assert db.get(Organisation, old) is not None
    assert store_row(db, store_id).status == DataStoreStatus.ready


@pytest.mark.parametrize(
    "kind", ["agent", "workflow", "skill", "chat", "schedule", "document", "table"]
)
def test_any_one_thing_in_the_organisation_refuses_accepting(api, db, user, joiner, kind):
    link = make_link(api)
    store_id = store_of(joiner)
    with signed_in_as(joiner):
        if kind == "agent":
            make_agent(api, "Helper")
        if kind == "schedule":
            # Of the documents assistant, which alone wouldn't refuse it (a chat made it).
            chat = make_chat(api, "scratch")
            assert api.delete(f"/api/chats/{chat['id']}").status_code == 204
            assistant = db.scalar(
                select(Agent.id).where(
                    Agent.organisation_id == organisations.membership_of(db, joiner.id).id,
                    Agent.builtin.is_not(None),
                )
            )
            make_trigger(api, schedule("agent", assistant))
        if kind == "workflow":
            make_workflow(api, "Flow")
        if kind == "skill":
            make_skill(api, "Notes")
        if kind == "chat":
            make_chat(api, "general")
        if kind == "document":
            add_document(store_id)
        if kind == "table":
            with as_owner(store_row(db, store_id)) as conn:
                conn.execute("CREATE TABLE data.fish (name text)")

        assert refusal(api, link)["contents"] == [{"kind": kind, "count": 1}]


def test_accepting_while_others_are_members_is_refused_as_before(api, db, user, joiner):
    link = make_link(api)
    other = join_organisation(db, joiner, "joiners-friend")
    try:
        with signed_in_as(joiner):
            assert refusal(api, link) == (
                "You're a member of joiner Tester. You can be in only one organisation at a time."
            )
    finally:
        db.execute(delete(User).where(User.id == other.id))
        db.commit()


def test_accepting_from_an_organisation_that_isnt_personal_is_refused_as_before(
    api, db, user, joiner
):
    link = make_link(api)
    db.execute(
        update(Organisation)
        .where(Organisation.id == organisations.membership_of(db, joiner.id).id)
        .values(personal=False)
    )
    db.commit()
    with signed_in_as(joiner):
        assert refusal(api, link) == (
            "You're a member of joiner Tester. You can be in only one organisation at a time."
        )
    with SessionLocal() as check:
        assert organisations.membership_of(check, joiner.id).name == "joiner Tester"
