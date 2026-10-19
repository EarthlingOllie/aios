"""Invitation links into an organisation, joining through one and leaving
(`aios.organisations`, `aios.api.organisation`)."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from conftest import delete_user, sign_up, store_of
from sqlalchemy import delete, func, select, update
from test_chats import signed_in_as
from test_schema_change_requests import CONTACTS

from aios import notifications, organisations
from aios.auth import current_user
from aios.datastores import schema_changes
from aios.datastores.schema_changes import SchemaChange
from aios.main import app
from aios.models import (
    ChatMember,
    DataStore,
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
    """Delete a user made here, with the personal organisations they're in (but not one they
    joined, which the test's `user` owns)."""
    db.expire_all()
    org = organisations.membership_of(db, u.id)
    if org is not None and org.personal and org.created_by == u.id:
        delete_user(db, u)
    else:
        db.delete(db.get(User, u.id))
        db.commit()


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = User(github_id=-2, login="member", name="Member Person")
    db.add(u)
    db.flush()
    db.add(
        OrganisationMember(
            organisation_id=organisations.membership_of(db, user.id).id, user_id=u.id
        )
    )
    db.commit()
    yield u
    leave_behind(db, u)


def make_link(api, **body) -> dict:
    resp = api.post("/api/organisation/invites", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_the_creator_makes_a_link_shown_once_and_only_its_hash_is_kept(api, db, user):
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


def test_only_the_creator_makes_lists_and_revokes_links(api, member):
    link = make_link(api)
    with signed_in_as(member):
        for resp in (
            api.post("/api/organisation/invites", json={}),
            api.get("/api/organisation/invites"),
            api.delete(f"/api/organisation/invites/{link['id']}"),
        ):
            assert resp.status_code == 403
            assert resp.json()["detail"] == (
                "Only the organisation's creator can invite people into it."
            )
    assert len(api.get("/api/organisation/invites").json()) == 1


def test_a_link_of_another_organisation_cant_be_revoked(api, new_user):
    other = new_user()
    with signed_in_as(other):
        theirs = make_link(api)
    assert api.delete(f"/api/organisation/invites/{theirs['id']}").status_code == 404
    with signed_in_as(other):
        assert len(api.get("/api/organisation/invites").json()) == 1


def by_github_id(db, github_id: int) -> User | None:
    db.expire_all()
    return db.scalar(select(User).where(User.github_id == github_id))


def test_signing_up_through_a_link_joins_the_organisation_and_makes_no_personal_one(
    api, db, user, monkeypatch
):
    from test_organisations import sign_in

    link = make_link(api)
    with signed_out():
        try:
            # Opening the invite page signed out, they sign in from it.
            opened = api.get(f"/login?invite={link['token']}", follow_redirects=False)
            assert opened.status_code == 302
            resp = sign_in(api, monkeypatch, -46, "joiner")
            assert resp.status_code == 303
            assert resp.headers["location"] == "http://localhost:5173"
            joiner = by_github_id(db, -46)
            org = organisations.membership_of(db, joiner.id)
            assert org.id == organisations.membership_of(db, user.id).id
            assert db.scalar(select(Organisation).where(Organisation.name == "joiner")) is None
        finally:
            if (joiner := by_github_id(db, -46)) is not None:
                leave_behind(db, joiner)
            api.cookies.clear()


def test_the_member_list_says_which_link_each_member_came_through(api, db, user, monkeypatch):
    from test_organisations import sign_in

    link = make_link(api)
    with signed_out():
        api.get(f"/login?invite={link['token']}", follow_redirects=False)
        sign_in(api, monkeypatch, -47, "joiner")
        api.cookies.clear()
    try:
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["login"], m["invite_id"]) for m in members] == [
            ("test-user", None),
            ("joiner", link["id"]),
        ]
    finally:
        leave_behind(db, by_github_id(db, -47))


def test_signing_up_through_an_expired_link_makes_a_personal_organisation_and_says_why(
    api, db, monkeypatch
):
    from test_organisations import sign_in

    link = make_link(api)
    api.delete(f"/api/organisation/invites/{link['id']}")
    with signed_out():
        try:
            api.get(f"/login?invite={link['token']}", follow_redirects=False)
            resp = sign_in(api, monkeypatch, -48, "latecomer")
            # Back to the invite page, which says the link no longer opens.
            assert resp.headers["location"] == f"http://localhost:5173/invite/{link['token']}"
            org = organisations.membership_of(db, by_github_id(db, -48).id)
            assert (org.name, org.personal) == ("latecomer", True)
        finally:
            if (u := by_github_id(db, -48)) is not None:
                delete_user(db, u)
            api.cookies.clear()


def test_signing_in_through_a_link_while_in_an_organisation_stays_in_it(api, db, monkeypatch):
    from test_organisations import sign_in

    link = make_link(api)
    existing = sign_up(db, -49, "existing")
    try:
        with signed_out():
            api.get(f"/login?invite={link['token']}", follow_redirects=False)
            resp = sign_in(api, monkeypatch, -49, "existing")
            # The invite page, where accepting says why they can't.
            assert resp.headers["location"] == f"http://localhost:5173/invite/{link['token']}"
            api.cookies.clear()
        assert organisations.membership_of(db, existing.id).name == "existing"
    finally:
        delete_user(db, existing)


def test_accepting_while_in_another_organisation_is_refused(api, user, new_user):
    link = make_link(api)
    other = new_user()
    with signed_in_as(other):
        resp = api.post(f"/api/invites/{link['token']}/accept")
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            f"You're a member of {other.login}. You can be in only one organisation at a time."
        )
        assert api.get("/api/me").json()["organisation"]["name"] == other.login
    # And the creator, already in it.
    resp = api.post(f"/api/invites/{link['token']}/accept")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "You're already a member of test-user."


def test_a_user_in_no_organisation_joins_by_accepting(api, db, user, new_user):
    link = make_link(api)
    stray = new_user()
    # Their membership removed by other means.
    org = organisations.membership_of(db, stray.id)
    db.delete(org)
    db.commit()
    try:
        with signed_in_as(stray):
            resp = api.post(f"/api/invites/{link['token']}/accept")
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "test-user"
            assert api.get("/api/me").json()["organisation"]["name"] == "test-user"
        members = api.get("/api/organisation").json()["members"]
        assert [(m["user"]["id"], m["invite_id"]) for m in members] == [
            (user.id, None),
            (stray.id, link["id"]),
        ]
    finally:
        # So deleting them afterwards doesn't delete the organisation they joined.
        db.execute(delete(OrganisationMember).where(OrganisationMember.user_id == stray.id))
        db.commit()


def test_a_member_leaves_for_a_new_personal_organisation_of_their_own(api, db, user, member):
    from test_chats import make_chat

    elsewhere = sign_up(db, -50, "elsewhere")
    try:
        ours = make_chat(api, "ours", visibility="public")
        with signed_in_as(elsewhere):
            theirs = make_chat(api, "theirs")
        store_id = store_of(user)
        # A request of the organisation's store, which notifies every member.
        request = schema_changes.propose(
            store_id,
            SchemaChange.model_validate({"ops": [CONTACTS]}),
            "To keep track of people",
            run_id=None,
            tool_call_id=None,
        )
        about = notifications.SCHEMA_CHANGE_REQUEST
        notifications.notify(db, [member.id], about, {"request_id": "2", "data_store_id": "other"})
        # One they've read already no longer reaches them, and stays.
        notifications.notify(
            db, [member.id], about, {"request_id": "3", "data_store_id": str(store_id)}
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
        db.add(ChatMember(chat_id=theirs["id"], kind="user", user_id=member.id, role="member"))
        db.commit()

        with signed_in_as(member):
            resp = api.post("/api/organisation/leave")
            assert resp.status_code == 200, resp.text
            mine = resp.json()
            assert (mine["name"], mine["personal"], mine["is_creator"]) == ("member", True, True)
            assert [m["user"]["id"] for m in mine["members"]] == [member.id]
            assert api.get("/api/me").json()["organisation"]["id"] == mine["id"]
            # With a store of its own, as signing up gives a new organisation.
            assert db.scalar(
                select(DataStore.id).where(DataStore.owner_organisation_id == mine["id"])
            ) not in (None, store_id)

        # Gone from the organisation and its chats, and its requests no longer reach them.
        members = api.get("/api/organisation").json()["members"]
        assert [m["user"]["id"] for m in members] == [user.id]
        db.expire_all()
        chats = db.scalars(select(ChatMember.chat_id).where(ChatMember.user_id == member.id))
        assert list(chats) == [theirs["id"]]
        refs = db.scalars(select(Notification.ref).where(Notification.user_id == member.id))
        assert sorted(r["request_id"] for r in refs) == ["2", "3"]
        # The members who stay are still notified of it.
        refs = db.scalars(select(Notification.ref).where(Notification.user_id == user.id))
        assert [r["request_id"] for r in refs] == [str(request.id)]
    finally:
        delete_user(db, elsewhere)


def test_the_last_member_cant_leave(api, user):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You're its only member, so nobody would be left to see what's in it: an organisation "
        "can't be left empty."
    )
    assert api.get("/api/me").json()["organisation"]["name"] == "test-user"


def test_the_creator_cant_leave_while_others_remain(api, user, member):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You created this organisation, and only its creator can invite people into it, so you "
        "can't leave while others are still members."
    )
    assert len(api.get("/api/organisation").json()["members"]) == 2
