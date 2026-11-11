"""Invitation links into an organisation, joining through one and leaving
(`aios.organisations`, `aios.api.organisation`)."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from conftest import (
    TEST_PASSWORD,
    delete_user,
    join_organisation,
    make_user,
    org_of,
    provisioned_user,
    register,
    rows_of,
    store_of,
)
from sqlalchemy import delete, func, select, update
from test_chats import signed_in_as
from test_organisations import in_the_background
from test_schema_change_requests import CONTACTS

from aios import notifications
from aios.auth import current_user
from aios.datastores import schema_changes
from aios.datastores.models import ChatMember
from aios.datastores.schema_changes import SchemaChange
from aios.main import app
from aios.models import (
    Notification,
    Organisation,
    OrganisationInvite,
    OrganisationMember,
    User,
)


@contextmanager
def signed_out():
    """The `api` client's requests come from nobody within the block."""
    previous = app.dependency_overrides.pop(current_user)
    try:
        yield
    finally:
        app.dependency_overrides[current_user] = previous


def leave_behind(db, u: User) -> None:
    """Delete a user made here, with the organisations they created (but not ones they joined,
    which other users own)."""
    db.expire_all()
    db.execute(
        delete(OrganisationMember).where(
            OrganisationMember.user_id == u.id,
            OrganisationMember.organisation_id.not_in(
                select(Organisation.id).where(Organisation.created_by == u.id)
            ),
        )
    )
    db.commit()
    delete_user(db, db.get_one(User, u.id))


def memberships(db, u: User) -> list[int]:
    """The ids of the organisations the user is in, by when they joined each."""
    db.expire_all()
    return list(
        db.scalars(
            select(OrganisationMember.organisation_id)
            .where(OrganisationMember.user_id == u.id)
            .order_by(OrganisationMember.joined_at, OrganisationMember.organisation_id)
        )
    )


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    leave_behind(db, u)


def make_link(api, **body) -> dict:
    resp = api.post("/api/organisation/invites", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_an_admin_makes_a_link_shown_once_and_only_its_hash_is_kept(api, db, user):
    link = make_link(api)
    assert link["url"] == f"http://localhost:5173/invite/{link['token']}"
    assert link["creator"]["id"] == user.id
    lasts = datetime.fromisoformat(link["expires_at"]) - datetime.fromisoformat(link["created_at"])
    assert lasts == timedelta(days=7)

    row = db.get(OrganisationInvite, link["id"])
    assert row.token_hash == hashlib.sha256(link["token"].encode()).hexdigest()
    assert link["token"] not in row.token_hash

    # Listed with who made it and when it expires, but never again with its token.
    [listed] = api.get("/api/organisation/invites").json()
    assert listed == {
        "id": link["id"],
        "role": "editor",
        "creator": link["creator"],
        "created_at": link["created_at"],
        "expires_at": link["expires_at"],
    }


def test_a_link_lasts_a_day_to_a_month(api):
    link = make_link(api, expires_in_days=30)
    lasts = datetime.fromisoformat(link["expires_at"]) - datetime.fromisoformat(link["created_at"])
    assert lasts == timedelta(days=30)
    assert make_link(api, expires_in_days=1)
    for days in (0, 31):
        resp = api.post("/api/organisation/invites", json={"expires_in_days": days})
        assert resp.status_code == 422


def test_opening_a_link_needs_no_sign_in_and_names_the_organisation(api, user):
    api.put("/api/organisation", json={"name": "Acme"})
    link = make_link(api)
    with signed_out():
        assert api.get("/api/me").status_code == 401
        resp = api.get(f"/api/invites/{link['token']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["organisation"]["name"] == "Acme"
        assert resp.json()["expires_at"] == link["expires_at"]
        assert api.get("/api/invites/not-a-token").status_code == 404


def test_a_revoked_link_no_longer_opens_or_lists(api):
    link, other = make_link(api), make_link(api)
    assert api.delete(f"/api/organisation/invites/{link['id']}").status_code == 204
    assert [i["id"] for i in api.get("/api/organisation/invites").json()] == [other["id"]]
    resp = api.get(f"/api/invites/{link['token']}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == (
        "This invitation link has expired or been revoked, or never existed."
    )
    assert api.post(f"/api/invites/{link['token']}/accept").status_code == 404
    # Revoking it again finds nothing.
    assert api.delete(f"/api/organisation/invites/{link['id']}").status_code == 404


def test_an_expired_link_no_longer_opens_or_lists(api, db):
    link = make_link(api)
    db.execute(
        update(OrganisationInvite)
        .where(OrganisationInvite.id == link["id"])
        .values(expires_at=datetime.fromisoformat(link["created_at"]) - timedelta(seconds=1))
    )
    db.commit()
    assert api.get(f"/api/invites/{link['token']}").status_code == 404
    assert api.post(f"/api/invites/{link['token']}/accept").status_code == 404
    assert api.get("/api/organisation/invites").json() == []


def test_only_an_admin_makes_lists_and_revokes_links(api, member):
    link = make_link(api)
    with signed_in_as(member):
        for resp in (
            api.post("/api/organisation/invites", json={}),
            api.get("/api/organisation/invites"),
            api.delete(f"/api/organisation/invites/{link['id']}"),
        ):
            assert resp.status_code == 403
            assert resp.json()["detail"] == (
                "Editors can't change the organisation's roles, invitations, name or settings, "
                "or delete it. Ask an admin."
            )
    assert len(api.get("/api/organisation/invites").json()) == 1


def test_a_link_of_another_organisation_cant_be_revoked(api, new_user):
    other = new_user()
    with signed_in_as(other):
        theirs = make_link(api)
    assert api.delete(f"/api/organisation/invites/{theirs['id']}").status_code == 404
    with signed_in_as(other):
        assert len(api.get("/api/organisation/invites").json()) == 1


def registered(db, resp) -> User:
    """The user a registration (`conftest.register`) made."""
    assert resp.status_code == 201, resp.text
    db.expire_all()
    return db.get(User, resp.json()["id"])


def test_registering_through_a_link_joins_only_that_organisation(api, db, user, provisioning):
    link = make_link(api)
    provisioning.clear()
    joiner = None
    with signed_out():
        try:
            # Opening the invite page signed out, they register from it.
            assert api.get(f"/api/invites/{link['token']}").status_code == 200
            resp = register(api, "Joiner", invite=link["token"])
            joiner = registered(db, resp)
            ours = org_of(db, user)
            assert memberships(db, joiner) == [ours.id]
            assert resp.json()["organisations"] == [
                {
                    "id": ours.id,
                    "name": ours.name,
                    "slug": ours.slug,
                    "is_creator": False,
                    "role": "editor",
                    "deletes_at": None,
                }
            ]
            assert (
                db.scalars(select(Organisation).where(Organisation.created_by == joiner.id)).all()
                == []
            )
            assert provisioning == []
        finally:
            if joiner is not None:
                leave_behind(db, joiner)
            api.cookies.clear()


def test_the_member_list_says_which_link_each_member_came_through(api, db, user):
    link = make_link(api)
    with signed_out():
        joiner = registered(db, register(api, "Joiner", invite=link["token"]))
        api.cookies.clear()
    try:
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["name"], m["invite_id"]) for m in members] == [
            ("Test User", None),
            ("Joiner Tester", link["id"]),
        ]
    finally:
        leave_behind(db, joiner)


def test_registering_through_a_revoked_link_joins_nothing(api, db):
    link = make_link(api)
    api.delete(f"/api/organisation/invites/{link['id']}")
    latecomer = None
    with signed_out():
        try:
            resp = register(api, "Latecomer", invite=link["token"])
            latecomer = registered(db, resp)
            assert resp.json()["organisations"] == []
            assert memberships(db, latecomer) == []
        finally:
            if latecomer is not None:
                delete_user(db, latecomer)
            api.cookies.clear()


def test_signing_in_from_a_links_page_joins_nothing(api, db):
    """Signing in never joins anyone: the invite page they're sent back to is where they accept
    the link."""
    link = make_link(api)
    existing = make_user(db, "existing")
    try:
        mine = memberships(db, existing)
        with signed_out():
            resp = api.post(
                "/api/auth/login",
                json={"email": existing.email, "password": TEST_PASSWORD, "invite": link["token"]},
            )
            assert resp.status_code == 200, resp.text
            api.cookies.clear()
        assert memberships(db, existing) == mine
    finally:
        delete_user(db, existing)


def test_a_member_of_one_organisation_accepts_a_link_to_another_and_is_in_both(
    api, db, user, new_user
):
    link = make_link(api)
    other = new_user()
    theirs, ours = org_of(db, other), org_of(db, user)
    try:
        with signed_in_as(other):
            resp = api.post(f"/api/invites/{link['token']}/accept")
            assert resp.status_code == 200, resp.text
            assert (resp.json()["id"], resp.json()["is_creator"]) == (ours.id, False)
            listed = api.get("/api/me").json()["organisations"]
        # The one joined most recently first.
        assert [(o["id"], o["is_creator"]) for o in listed] == [(ours.id, False), (theirs.id, True)]
        assert memberships(db, other) == [theirs.id, ours.id]
        # Their own organisation, and what's in it, is untouched.
        assert db.get(Organisation, theirs.id) is not None
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["id"], m["invite_id"]) for m in members] == [
            (user.id, None),
            (other.id, link["id"]),
        ]
    finally:
        db.execute(
            delete(OrganisationMember).where(
                OrganisationMember.user_id == other.id,
                OrganisationMember.organisation_id == ours.id,
            )
        )
        db.commit()


def test_accepting_the_same_link_twice_answers_200_and_adds_nothing(api, db, user, new_user):
    link = make_link(api)
    other = new_user()
    ours = org_of(db, user)
    try:
        with signed_in_as(other):
            first = api.post(f"/api/invites/{link['token']}/accept")
            again = api.post(f"/api/invites/{link['token']}/accept")
        assert (first.status_code, again.status_code) == (200, 200)
        assert again.json() == first.json()
        assert memberships(db, other) == [org_of(db, other).id, ours.id]
        # And the creator accepting a link into their own organisation: nothing changes.
        assert api.post(f"/api/invites/{link['token']}/accept").status_code == 200
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["id"], m["invite_id"]) for m in members] == [
            (user.id, None),
            (other.id, link["id"]),
        ]
    finally:
        db.execute(
            delete(OrganisationMember).where(
                OrganisationMember.user_id == other.id,
                OrganisationMember.organisation_id == ours.id,
            )
        )
        db.commit()


def test_two_accepts_at_once_both_succeed_with_one_membership(api, db, user, new_user):
    link = make_link(api)
    other = new_user()
    ours = org_of(db, user)
    try:
        with signed_in_as(other):
            runs = [
                in_the_background(lambda: api.post(f"/api/invites/{link['token']}/accept"))
                for _ in range(2)
            ]
            for thread, _ in runs:
                thread.join(timeout=10)
        for _, result in runs:
            assert "error" not in result, result
            assert result["value"].status_code == 200, result["value"].text
        assert memberships(db, other) == [org_of(db, other).id, ours.id]
    finally:
        db.execute(
            delete(OrganisationMember).where(
                OrganisationMember.user_id == other.id,
                OrganisationMember.organisation_id == ours.id,
            )
        )
        db.commit()


def test_a_person_in_no_organisation_joins_by_accepting(api, db, user):
    link = make_link(api)
    with signed_out():
        stray = registered(db, register(api, "Stray"))
        try:
            assert api.get("/api/me").json()["organisations"] == []
            resp = api.post(f"/api/invites/{link['token']}/accept")
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "Test User"
            listed = api.get("/api/me").json()["organisations"]
            assert [o["name"] for o in listed] == ["Test User"]
        finally:
            api.cookies.clear()
    try:
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["id"], m["invite_id"]) for m in members] == [
            (user.id, None),
            (stray.id, link["id"]),
        ]
    finally:
        leave_behind(db, stray)


def test_a_member_leaves_and_only_their_membership_goes(api, db, user, member, provisioning):
    from test_chats import make_chat

    # Making a chat makes its documents assistant, in the organisation's store.
    elsewhere = provisioned_user(db, "elsewhere")
    try:
        ours = make_chat(api, "ours", visibility="public")
        with signed_in_as(elsewhere):
            theirs = make_chat(api, "theirs")
        store_id = store_of(user)
        provisioning.clear()
        # A request of the organisation's store, which notifies every member.
        request = schema_changes.propose(
            store_id,
            SchemaChange.model_validate({"ops": [CONTACTS]}),
            "To keep track of people",
            run_id=None,
            tool_call_id=None,
        )
        about = notifications.SCHEMA_CHANGE_REQUEST
        other = org_of(db, elsewhere).id
        notifications.notify(
            db, [member.id], other, about, {"request_id": "2", "data_store_id": "other"}
        )
        # One they've read already no longer reaches them, and stays.
        notifications.notify(
            db,
            [member.id],
            org_of(db, user).id,
            about,
            {"request_id": "3", "data_store_id": str(store_id)},
        )
        db.flush()
        db.execute(
            update(Notification)
            .where(Notification.user_id == member.id, Notification.ref["request_id"].astext == "3")
            .values(read_at=func.now())
        )
        db.commit()
        with signed_in_as(member):
            assert api.post(f"/api/chats/{ours['id']}/join").status_code == 200
        rows_of(elsewhere).add(
            ChatMember(chat_id=theirs["id"], kind="user", user_id=member.id, role="member")
        )

        with signed_in_as(member):
            resp = api.post("/api/organisation/leave")
            assert resp.status_code == 200, resp.text
            # In no organisation now: nothing was made for them.
            assert resp.json()["organisations"] == []
            assert api.get("/api/me").json() == resp.json()
        assert memberships(db, member) == []
        made = select(Organisation).where(Organisation.created_by == member.id)
        assert db.scalars(made).all() == []
        assert provisioning == []

        # Gone from the organisation and its chats, and its requests no longer reach them.
        members = api.get("/api/organisation").json()["members"]
        assert [m["user"]["id"] for m in members] == [user.id]
        db.expire_all()
        theirs_now = select(ChatMember.chat_id).where(ChatMember.user_id == member.id)
        assert rows_of(user).scalars(theirs_now) == []
        assert rows_of(elsewhere).scalars(theirs_now) == [theirs["id"]]
        refs = db.scalars(select(Notification.ref).where(Notification.user_id == member.id))
        assert sorted(r["request_id"] for r in refs) == ["2", "3"]
        # The members who stay are still notified of it.
        refs = db.scalars(select(Notification.ref).where(Notification.user_id == user.id))
        assert [r["request_id"] for r in refs] == [str(request.id)]
    finally:
        delete_user(db, elsewhere)


def test_leaving_one_of_several_organisations_keeps_the_others(api, db, user, new_user):
    link = make_link(api)
    other = new_user()
    theirs = org_of(db, other)
    with signed_in_as(other):
        assert api.post(f"/api/invites/{link['token']}/accept").status_code == 200
        ours = org_of(db, user)
        resp = api.post(f"/api/o/{ours.id}/organisation/leave")
        assert resp.status_code == 200, resp.text
        assert [o["id"] for o in resp.json()["organisations"]] == [theirs.id]
    assert memberships(db, other) == [theirs.id]
    members = api.get("/api/organisation").json()["members"]
    assert [m["user"]["id"] for m in members] == [user.id]


def test_the_last_member_is_told_to_delete_the_organisation_instead(api, db, user):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You're its only member, so nobody would be left to see what's in it. Delete the "
        "organisation instead."
    )
    assert memberships(db, user) == [org_of(db, user).id]


def test_the_last_admin_cant_leave_while_others_remain(api, user, member):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You're its only admin, and an organisation needs an admin, so you can't leave while "
        "others are still members. Make someone else an admin first."
    )
    assert len(api.get("/api/organisation").json()["members"]) == 2
