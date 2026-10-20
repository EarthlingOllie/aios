"""Invitation links into an organisation, joining through one and leaving
(`aios.organisations`, `aios.api.organisation`)."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from conftest import (
    TEST_PASSWORD,
    build_user,
    delete_user,
    join_organisation,
    make_user,
    register,
    store_of,
)
from sqlalchemy import delete, func, select, update
from test_chats import signed_in_as
from test_organisations import in_the_background, picking_names_at_most
from test_schema_change_requests import CONTACTS

from aios import notifications, organisations
from aios.auth import current_user
from aios.datastores import schema_changes
from aios.datastores.schema_changes import SchemaChange
from aios.db import SessionLocal
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
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
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


def registered(db, resp) -> User:
    """The user a registration (`conftest.register`) made."""
    assert resp.status_code == 201, resp.text
    db.expire_all()
    return db.get(User, resp.json()["id"])


def test_registering_through_a_link_joins_the_organisation_and_makes_no_personal_one(api, db, user):
    link = make_link(api)
    joiner = None
    with signed_out():
        try:
            # Opening the invite page signed out, they register from it.
            assert api.get(f"/api/invites/{link['token']}").status_code == 200
            resp = register(api, "Joiner", invite=link["token"])
            joiner = registered(db, resp)
            org = organisations.membership_of(db, joiner.id)
            assert org.id == organisations.membership_of(db, user.id).id
            assert resp.json()["organisation"]["id"] == org.id
            assert (
                db.scalar(select(Organisation).where(Organisation.name == "Joiner Tester")) is None
            )
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


def test_registering_through_a_revoked_link_makes_a_personal_organisation(api, db):
    link = make_link(api)
    api.delete(f"/api/organisation/invites/{link['id']}")
    latecomer = None
    with signed_out():
        try:
            latecomer = registered(db, register(api, "Latecomer", invite=link["token"]))
            org = organisations.membership_of(db, latecomer.id)
            assert (org.name, org.personal) == ("Latecomer Tester", True)
        finally:
            if latecomer is not None:
                delete_user(db, latecomer)
            api.cookies.clear()


def test_signing_in_from_a_links_page_while_in_an_organisation_stays_in_it(api, db):
    """Signing in never moves anyone: the invite page they're sent back to is where they accept
    the link, or are told why they can't."""
    link = make_link(api)
    existing = make_user(db, "existing")
    try:
        with signed_out():
            resp = api.post(
                "/api/auth/login",
                json={"email": existing.email, "password": TEST_PASSWORD, "invite": link["token"]},
            )
            assert resp.status_code == 200, resp.text
            api.cookies.clear()
        assert organisations.membership_of(db, existing.id).name == "existing Tester"
    finally:
        delete_user(db, existing)


def test_accepting_while_in_another_organisation_is_refused(api, db, user, new_user):
    link = make_link(api)
    other = new_user()
    # Not theirs alone: an empty organisation of their own would be deleted as they join
    # (`test_empty_organisation`).
    friend = join_organisation(db, other, "friend")
    try:
        with signed_in_as(other):
            resp = api.post(f"/api/invites/{link['token']}/accept")
            assert resp.status_code == 409
            assert resp.json()["detail"] == (
                f"You're a member of {other.full_name}. You can be in only one organisation at a "
                "time."
            )
            assert api.get("/api/me").json()["organisation"]["name"] == other.full_name
    finally:
        db.delete(friend)
        db.commit()
    # And the creator, already in it.
    resp = api.post(f"/api/invites/{link['token']}/accept")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "You're already a member of Test User."


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
            assert resp.json()["name"] == "Test User"
            assert api.get("/api/me").json()["organisation"]["name"] == "Test User"
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

    elsewhere = make_user(db, "elsewhere")
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
            assert (mine["name"], mine["personal"], mine["is_creator"]) == (
                "Member Person",
                True,
                True,
            )
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


def test_leaving_while_a_sign_up_takes_the_name_lands_in_a_suffixed_one(api, db, user, member):
    """Someone else with the member's name signs up as they leave: the name looked free to the
    leave, and is taken by the time it inserts."""
    twin = build_user("twin", first_name="Member", last_name="Person")
    try:
        with SessionLocal() as signing_up:
            # The sign-up, flushed but not committed: the leave waits on its name.
            signing_up.add(twin)
            signing_up.flush()
            organisations.create_personal(signing_up, twin)
            with signed_in_as(member):
                leaving, result = in_the_background(lambda: api.post("/api/organisation/leave"))
                leaving.join(timeout=0.5)
                assert leaving.is_alive(), "the leave should wait on the sign-up's name"
                signing_up.commit()
                leaving.join(timeout=10)

        assert "error" not in result, result
        resp = result["value"]
        assert resp.status_code == 200, resp.text
        mine = resp.json()
        assert (mine["name"], mine["personal"], mine["is_creator"]) == (
            "Member Person-2",
            True,
            True,
        )
        assert [m["user"]["id"] for m in mine["members"]] == [member.id]
        # The rest of the leave held: they're out of the organisation they left.
        members = api.get("/api/organisation").json()["members"]
        assert [m["user"]["id"] for m in members] == [user.id]
    finally:
        if twin.id is not None and (twin := db.get(User, twin.id)) is not None:
            delete_user(db, twin)


def test_leaving_with_a_name_with_like_wildcards_or_a_backslash_lands_in_the_next_free_name(
    api, db, member, monkeypatch
):
    """`_`, `%` and `\\` mean something to LIKE. A member whose name has them sees the names
    they could take (their name, and it suffixed) as taken when they leave, and only those."""
    member.first_name = "mem\\_%"
    name = member.full_name
    others = [name, f"{name}-2", "mem__x Person", "mem__x Person-3"]
    try:
        db.add_all(Organisation(name=n) for n in others)
        db.commit()
        # One pick: no clash to pick again after.
        picking_names_at_most(monkeypatch, 1)
        with signed_in_as(member):
            resp = api.post("/api/organisation/leave")
        assert resp.status_code == 200, resp.text
        assert (resp.json()["name"], resp.json()["is_creator"]) == (f"{name}-3", True)
    finally:
        db.execute(delete(Organisation).where(Organisation.name.in_(others)))
        db.commit()


def test_the_last_member_cant_leave(api, user):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You're its only member, so nobody would be left to see what's in it: an organisation "
        "can't be left empty."
    )
    assert api.get("/api/me").json()["organisation"]["name"] == "Test User"


def test_the_creator_cant_leave_while_others_remain(api, user, member):
    resp = api.post("/api/organisation/leave")
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You created this organisation, and only its creator can invite people into it, so you "
        "can't leave while others are still members."
    )
    assert len(api.get("/api/organisation").json()["members"]) == 2
