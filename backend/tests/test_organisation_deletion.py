"""Deleting an organisation (`aios.organisations`, `aios.api.organisation`): an admin schedules
it by typing its name, every member is told, and after a grace period the background sweep
records it, detaches its store for the store teardown and deletes it. Until then it works as
before, but its invitation links don't open, and an admin can cancel."""

from datetime import datetime, timedelta

import pytest
from conftest import delete_user, join_organisation, make_user, org_of
from sqlalchemy import delete, select
from test_chats import signed_in_as
from test_invitations import signed_out

from aios import organisations
from aios.config import Settings, get_settings
from aios.datastores import provision
from aios.models import DataStore, DataStoreStatus, Notification, Organisation, OrganisationDeletion
from aios.worker import celery_app

DELETION = "/api/organisation/deletion"


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    delete_user(db, u)


@pytest.fixture
def store_deletions(monkeypatch) -> list:
    """The stores whose teardown the sweep queued, instead of sending it to the broker."""
    queued: list = []
    monkeypatch.setattr(organisations, "enqueue_store_deletion", queued.append)
    return queued


@pytest.fixture
def no_grace(monkeypatch):
    """A grace period that's over as soon as a deletion is scheduled."""
    monkeypatch.setattr(get_settings(), "organisation_deletion_grace_days", 0)


def schedule(api, name: str = "Test User"):
    return api.post(DELETION, json={"name": name})


def notifications_of(db, user) -> list[Notification]:
    db.expire_all()
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at, Notification.kind)
        )
    )


def parse(at: str) -> datetime:
    return datetime.fromisoformat(at)


# --- Scheduling ----------------------------------------------------------------------------------


def test_the_creator_schedules_deletion_by_typing_the_name_and_every_member_is_told(
    api, db, user, member
):
    resp = schedule(api)
    assert resp.status_code == 200, resp.text
    deletion = resp.json()["deletion"]
    assert deletion["scheduled_by"] == {"id": user.id, "name": "Test User", "former_member": False}
    # The grace period: 14 days by default.
    assert parse(deletion["deletes_at"]) - parse(deletion["scheduled_at"]) == timedelta(days=14)

    org = org_of(db, user)
    for u in (user, member):
        [told] = notifications_of(db, u)
        assert (told.kind, told.organisation_id) == ("organisation_deletion_scheduled", org.id)
        # About the organisation itself: its name is read when it's listed.
        assert told.ref == {}
        # The banner's date: the organisation as "me" lists it, from either seat.
        with signed_in_as(u):
            [listed] = api.get("/api/me").json()["organisations"]
            assert parse(listed["deletes_at"]) == parse(deletion["deletes_at"])
            assert api.get("/api/organisation").json()["deletion"] == deletion
    with signed_in_as(member):
        [listed] = api.get("/api/me/notifications").json()["notifications"]
    assert listed["kind"] == "organisation_deletion_scheduled"
    assert listed["title"] == "Test User is scheduled for deletion."


def test_a_name_other_than_the_organisations_is_refused(api, db, user):
    for wrong in ("Test", "test user", "Someone Else"):
        resp = schedule(api, wrong)
        assert resp.status_code == 422, wrong
        assert resp.json()["detail"] == "That isn't the organisation's name."
    assert api.get("/api/organisation").json()["deletion"] is None
    assert notifications_of(db, user) == []


def test_scheduling_twice_is_a_conflict(api, db, user):
    first = schedule(api).json()["deletion"]
    resp = schedule(api)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The organisation is already scheduled for deletion."
    assert api.get("/api/organisation").json()["deletion"] == first
    assert len(notifications_of(db, user)) == 1


def test_only_an_admin_can_schedule_or_cancel(api, db, user, member):
    with signed_in_as(member):
        resp = schedule(api)
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "Editors can't change the organisation's roles, invitations, name or settings, or "
        "delete it. Ask an admin."
    )
    assert api.get("/api/organisation").json()["deletion"] is None

    schedule(api)
    with signed_in_as(member):
        resp = api.delete(DELETION)
    assert resp.status_code == 403
    assert api.get("/api/organisation").json()["deletion"] is not None


def test_the_grace_period_and_check_interval_come_from_settings():
    defaults = Settings()
    assert defaults.organisation_deletion_grace_days == 14
    assert defaults.organisation_deletion_check_seconds == 10 * 60
    sweep = celery_app.conf.beat_schedule["delete-due-organisations"]
    assert sweep["task"] == "aios.delete_due_organisations"
    assert sweep["schedule"] == get_settings().organisation_deletion_check_seconds


def test_the_grace_period_setting_sets_the_date(api, monkeypatch, user):
    monkeypatch.setattr(get_settings(), "organisation_deletion_grace_days", 2.5)
    deletion = schedule(api).json()["deletion"]
    gap = parse(deletion["deletes_at"]) - parse(deletion["scheduled_at"])
    assert gap == timedelta(days=2, hours=12)


# --- During the grace period ---------------------------------------------------------------------


def test_the_organisation_works_as_before_during_the_grace_period(api, store_deletions, user):
    schedule(api)
    resp = api.post("/api/skills", json={"name": "Still here", "description": "d", "content": "c"})
    assert resp.status_code == 201, resp.text
    assert organisations.delete_due() == []
    assert api.get("/api/skills").status_code == 200
    assert store_deletions == []


def test_invitation_links_stop_opening_once_deletion_is_scheduled(api, db, user):
    token = api.post("/api/organisation/invites", json={}).json()["token"]
    schedule(api)
    with signed_out():
        assert api.get(f"/api/invites/{token}").status_code == 404
    joiner = make_user(db, "joiner")
    try:
        with signed_in_as(joiner):
            assert api.post(f"/api/invites/{token}/accept").status_code == 404
            assert [o["name"] for o in api.get("/api/me").json()["organisations"]] == [
                joiner.full_name
            ]
        # Cancelled, they open again (until they expire).
        assert api.delete(DELETION).status_code == 200
        with signed_out():
            assert api.get(f"/api/invites/{token}").status_code == 200
    finally:
        delete_user(db, joiner)


# --- Cancelling ----------------------------------------------------------------------------------


def test_cancelling_keeps_everything_and_tells_every_member_again(
    api, db, store_deletions, user, member
):
    schedule(api)
    resp = api.delete(DELETION)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deletion"] is None
    with signed_in_as(member):
        [listed] = api.get("/api/me").json()["organisations"]
    assert listed["deletes_at"] is None
    for u in (user, member):
        kinds = [n.kind for n in notifications_of(db, u)]
        assert sorted(kinds) == [
            "organisation_deletion_cancelled",
            "organisation_deletion_scheduled",
        ]
    assert organisations.delete_due() == []
    assert db.get(Organisation, org_of(db, user).id) is not None
    assert store_deletions == []


def test_cancelling_what_isnt_scheduled_is_a_conflict(api):
    resp = api.delete(DELETION)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The organisation isn't scheduled for deletion."


# --- Deleting ------------------------------------------------------------------------------------


def test_after_the_grace_period_the_sweep_deletes_it_records_it_and_tears_its_store_down(
    api, db, store_deletions, no_grace, user, member
):
    org = org_of(db, user)
    org_id, slug = org.id, org.slug
    store = db.scalar(select(DataStore).where(DataStore.owner_organisation_id == org_id))
    store_id = store.id
    # Things in it, some pinning others' versions.
    skill = api.post("/api/skills", json={"name": "S", "description": "d", "content": "c"}).json()
    agent = api.post(
        "/api/agents",
        json={
            "name": "A",
            "description": "d",
            "base_prompt": "p",
            "skill_version_ids": [skill["current_version"]["id"]],
        },
    )
    assert agent.status_code == 201, agent.text
    assert api.post("/api/chats", json={"name": "general"}).status_code == 201
    scheduled = schedule(api).json()["deletion"]

    try:
        assert organisations.delete_due() == [org_id]

        db.expire_all()
        assert db.get(Organisation, org_id) is None
        # Its agents, skills and workflows were in its store, which goes (below).
        record = db.scalar(
            select(OrganisationDeletion).where(OrganisationDeletion.organisation_id == org_id)
        )
        assert (record.name, record.slug, record.scheduled_by) == ("Test User", slug, user.id)
        assert record.scheduled_at == parse(scheduled["scheduled_at"])
        assert record.deleted_at >= record.scheduled_at
        assert record.data_store_id == store_id

        # Its store is detached, for the existing teardown, which is queued.
        db.refresh(store)
        assert (store.status, store.owner_organisation_id) == (DataStoreStatus.deleting, None)
        assert store_deletions == [store_id]
        provision.delete_deleting([store_id], wait=True)
        db.refresh(store)
        assert store.status == DataStoreStatus.deleted

        # Its members are in no organisation now: the app sends them to the welcome page.
        for u in (user, member):
            with signed_in_as(u):
                assert api.get("/api/me").json()["organisations"] == []
                assert api.get(f"/api/o/{org_id}/organisation").status_code == 404
        # Swept again, there's nothing left to do.
        assert organisations.delete_due() == []
    finally:
        db.execute(
            delete(OrganisationDeletion).where(OrganisationDeletion.organisation_id == org_id)
        )
        db.execute(delete(DataStore).where(DataStore.id == store_id))
        db.commit()


def test_a_member_of_other_organisations_keeps_them(
    api, db, store_deletions, no_grace, user, member
):
    with signed_in_as(member):
        other = api.post("/api/organisations", json={"name": "Elsewhere"}).json()
    org_id = org_of(db, user).id
    schedule(api)
    try:
        assert organisations.delete_due() == [org_id]
        with signed_in_as(member):
            listed = [o["id"] for o in api.get("/api/me").json()["organisations"]]
        assert listed == [other["id"]]
    finally:
        db.execute(
            delete(OrganisationDeletion).where(OrganisationDeletion.organisation_id == org_id)
        )
        db.commit()


# --- Names and "personal" ------------------------------------------------------------------------


def test_two_organisations_can_share_a_name_but_not_a_slug(api, user):
    first = api.post("/api/organisations", json={"name": "Shared Name"}).json()
    second = api.post("/api/organisations", json={"name": "Shared Name"})
    assert second.status_code == 201, second.text
    assert second.json()["name"] == "Shared Name"
    assert (first["slug"], second.json()["slug"]) == ("shared-name", "shared-name-2")
    # Renaming to a name another has is fine too.
    assert api.put("/api/organisation", json={"name": "Shared Name"}).status_code == 200


def test_personal_appears_nowhere_in_the_api(api, user):
    org = api.post("/api/organisations", json={"name": "Anything"}).json()
    for body in (org, api.get("/api/organisation").json(), api.get("/api/me").json()):
        assert "personal" not in repr(body)
    assert "personal" not in repr(api.get("/openapi.json").json()).lower()
