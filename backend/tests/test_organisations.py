"""Organisations: every user is in one (`aios.organisations`), shown by `/api/me` and the
Organisation endpoints."""

import importlib.util
import threading
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import (
    TEST_PASSWORD,
    build_user,
    delete_user,
    join_organisation,
    register,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update
from test_chats import signed_in_as

from aios import organisations
from aios.db import SessionLocal
from aios.main import app
from aios.models import Agent, Chat, ChatVisibility, Organisation, OrganisationMember, User

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def revision(name: str):
    """A migration module, by its file's stem, for its data step."""
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def before_the_contract(db) -> None:
    """Put the schema back as it was before the contract step (revision 0048's downgrade), in
    this test's transaction only: each row's owner is back, filled in from who made it."""
    with Operations.context(MigrationContext.configure(db.connection())):
        revision("0048_organisations_contract").downgrade()


def before_native_auth(db) -> None:
    """Put the schema back as it was before native auth (revision 0052's downgrade): GitHub's
    columns on `users`. In this test's transaction only, and with every user and organisation
    deleted in it, as the downgrade does."""
    with Operations.context(MigrationContext.configure(db.connection())):
        revision("0052_native_auth").downgrade()


def test_the_upgrade_gives_each_user_a_personal_organisation_and_stamps_what_they_own(db):
    """The data step (revision 0030), on users made before organisations existed (GitHub's, so
    before native auth too): two whose logins would name the same organisation, and one whose
    login is the suffixed name."""
    before_native_auth(db)
    first, second, third = (
        db.execute(
            text("INSERT INTO users (github_id, login) VALUES (:github_id, :login) RETURNING id"),
            {"github_id": github_id, "login": login},
        ).scalar_one()
        # A login is unique on GitHub at a time, but a user renamed since keeps their old one here.
        for github_id, login in ((-901, "sam"), (-902, "sam"), (-903, "sam-2"))
    )
    # What they own, as it was before organisations: made in an organisation of theirs, which
    # the contract's downgrade makes theirs to own, and the organisation then undone.
    made = []
    for user_id in (first, second):
        org = Organisation(name=f"made-{user_id}", personal=True, created_by=user_id)
        db.add(org)
        db.flush()
        db.add(OrganisationMember(organisation_id=org.id, user_id=user_id))
        made.append(org)
    agent = Agent(organisation_id=made[1].id, created_by=second, name="Reviewer")
    chat = Chat(
        organisation_id=made[0].id,
        created_by=first,
        name="design-0030",
        visibility=ChatVisibility.private,
    )
    db.add_all([agent, chat])
    db.flush()
    before_the_contract(db)
    for row in (agent, chat):
        db.execute(
            update(type(row))
            .where(type(row).id == row.id)
            .values(organisation_id=None, created_by=None)
        )
    db.execute(delete(Organisation).where(Organisation.id.in_([o.id for o in made])))

    revision("0030_personal_organisations").backfill(db.connection())

    def organisation_of(user_id: int) -> Organisation:
        return db.scalar(
            select(Organisation)
            .join(OrganisationMember)
            .where(OrganisationMember.user_id == user_id)
        )

    # Named after the login, suffixed on a clash; in the order the users signed up.
    names = [organisation_of(u).name for u in (first, second, third)]
    assert names == ["sam", "sam-2", "sam-2-2"]
    for u in (first, second, third):
        org = organisation_of(u)
        assert (org.personal, org.created_by) == (True, u)
        assert [m.user_id for m in org.members] == [u]

    db.refresh(agent)
    db.refresh(chat)
    assert (agent.organisation_id, agent.created_by) == (organisation_of(second).id, second)
    assert (chat.organisation_id, chat.created_by) == (organisation_of(first).id, first)
    # Nothing else is claimed: who last changed or deleted it isn't known.
    assert (agent.updated_by, agent.deleted_by) == (None, None)

    # Run again (a user already in an organisation), it makes no second one.
    revision("0030_personal_organisations").backfill(db.connection())
    assert (
        db.scalar(
            select(OrganisationMember.organisation_id).where(OrganisationMember.user_id == first)
        )
        == organisation_of(first).id
    )
    assert db.scalar(select(Organisation).where(Organisation.name == "sam-3")) is None


def registered(db, resp) -> User:
    """The user a registration (`conftest.register`) made."""
    assert resp.status_code == 201, resp.text
    return db.get(User, resp.json()["id"])


def test_registering_makes_a_personal_organisation_and_signing_in_finds_it(client, db):
    resp = register(client, "Newcomer")
    newcomer = registered(db, resp)
    try:
        [org] = db.scalars(
            select(Organisation)
            .join(OrganisationMember)
            .where(OrganisationMember.user_id == newcomer.id)
        ).all()
        assert (org.name, org.personal, org.created_by) == ("Newcomer Tester", True, newcomer.id)
        assert [m.user_id for m in org.members] == [newcomer.id]

        # Signing in again finds the same one.
        signing_in = TestClient(app).post(
            "/api/auth/login", json={"email": newcomer.email, "password": TEST_PASSWORD}
        )
        assert signing_in.json()["organisation"]["id"] == org.id
        db.expire_all()
        named = select(Organisation).where(Organisation.name.like("Newcomer Tester%"))
        assert db.scalars(named).all() == [org]
    finally:
        delete_user(db, newcomer)


def test_a_personal_organisation_named_after_a_taken_name_is_suffixed(client, db):
    twins = [registered(db, register(client, "Twin")) for _ in range(2)]
    try:
        assert [organisations.membership_of(db, u.id).name for u in twins] == [
            "Twin Tester",
            "Twin Tester-2",
        ]
    finally:
        for u in twins:
            delete_user(db, u)


def in_the_background(call) -> tuple[threading.Thread, dict]:
    """Start `call` on a thread; the dict gets its result (or the exception it raised)."""
    result: dict = {}

    def run():
        try:
            result["value"] = call()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_registering_while_a_rename_takes_the_name_lands_in_a_suffixed_one(client, db, user):
    """The creator of another organisation renames it to the user's name as they register: the
    name looked free to the registration, and is taken by the time it inserts."""
    result: dict = {}
    try:
        with SessionLocal() as renaming:
            # The rename, flushed but not committed: the registration waits on its name.
            organisations.membership_of(renaming, user.id).name = "Dana Tester"
            renaming.flush()
            registering, result = in_the_background(lambda: register(client, "Dana"))
            registering.join(timeout=0.5)
            assert registering.is_alive(), "the registration should wait on the rename's name"
            renaming.commit()
        registering.join(timeout=10)

        assert "error" not in result, result
        dana = registered(db, result["value"])
        org = organisations.membership_of(db, dana.id)
        assert (org.name, org.personal, org.created_by) == ("Dana Tester-2", True, dana.id)
    finally:
        if "value" in result and result["value"].status_code == 201:
            delete_user(db, db.get(User, result["value"].json()["id"]))


def picking_names_at_most(monkeypatch, times: int) -> None:
    """Fail `personal_name` past its `times`th call: a name it keeps picking that's taken
    would otherwise loop `create_personal` for ever, hanging the test instead of failing it."""
    pick, calls = organisations.personal_name, []

    def limited(db, base):
        calls.append(base)
        assert len(calls) <= times, f"picked a name for {base!r} {len(calls)} times"
        return pick(db, base)

    monkeypatch.setattr(organisations, "personal_name", limited)


def test_a_name_with_like_wildcards_or_a_backslash_is_suffixed_on_its_own_names_only(
    client, db, monkeypatch
):
    """`_`, `%` and `\\` mean something to LIKE. A name with them still sees the names it could
    take (itself, and itself suffixed) as taken, and only those: another name its wildcards
    would match takes no suffix from it."""
    name = "w_d%c\\ Tester"
    others = ["wxdyc\\ Tester", "wxdyc\\ Tester-3", f"{name}-2"]
    made: list[User] = []
    try:
        db.add_all(Organisation(name=n) for n in others)
        db.commit()
        # One pick for each registration: no clash to pick again after.
        picking_names_at_most(monkeypatch, 2)
        made = [registered(db, register(client, "w_d%c\\")) for _ in range(2)]
        names = [organisations.membership_of(db, u.id).name for u in made]
        assert names == [name, f"{name}-3"]
    finally:
        for u in made:
            delete_user(db, u)
        db.execute(delete(Organisation).where(Organisation.name.in_(others)))
        db.commit()


def test_me_says_which_organisation_the_user_is_in(api, user):
    me = api.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Test", "User")
    assert me["organisation"] == {
        "id": me["organisation"]["id"],
        "name": "Test User",
        "personal": True,
        "is_creator": True,
    }


@pytest.fixture
def member(db, user):
    """A second member of `user`'s organisation, who isn't its creator."""
    u = join_organisation(db, user, "member", first_name="Member", last_name="Person")
    yield u
    db.delete(u)
    db.commit()


def test_the_organisation_page_shows_the_name_creator_and_members_with_when_they_joined(
    api, user, member
):
    org = api.get("/api/organisation").json()
    assert (org["name"], org["personal"], org["is_creator"]) == ("Test User", True, True)
    assert org["creator"] == {"id": user.id, "name": "Test User"}
    assert [(m["user"], m["invite_id"]) for m in org["members"]] == [
        ({"id": user.id, "name": "Test User"}, None),
        ({"id": member.id, "name": "Member Person"}, None),
    ]
    first, second = (m["joined_at"] for m in org["members"])
    assert first <= second
    # The same organisation, from the other member's seat.
    with signed_in_as(member):
        theirs = api.get("/api/organisation").json()
    assert theirs == {**org, "is_creator": False}
    assert api.get("/api/me").json()["organisation"]["id"] == org["id"]


def test_the_creator_renames_the_organisation(api, user):
    resp = api.put("/api/organisation", json={"name": "  Acme  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Acme"
    # It stays personal: the flag says how it was made, not what it's called.
    assert resp.json()["personal"] is True
    assert api.get("/api/me").json()["organisation"]["name"] == "Acme"
    assert api.put("/api/organisation", json={"name": " "}).status_code == 422


def test_renaming_to_a_name_a_sign_up_is_taking_is_refused(api, db, user):
    """The creator renames to a name a new user's personal organisation is taking at the same
    moment: the rename is the one refused, as for a name already taken."""
    newcomer = build_user("acme")
    try:
        with SessionLocal() as signing_up:
            # The sign-up, flushed but not committed: the rename waits on its name.
            signing_up.add(newcomer)
            signing_up.flush()
            organisations.create_personal(signing_up, newcomer)
            renaming, result = in_the_background(
                lambda: api.put("/api/organisation", json={"name": "acme Tester"})
            )
            renaming.join(timeout=0.5)
            assert renaming.is_alive(), "the rename should wait on the sign-up's name"
            signing_up.commit()
        renaming.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 409
        assert result["value"].json()["detail"] == "An organisation with that name already exists."
        assert api.get("/api/organisation").json()["name"] == "Test User"
    finally:
        if (newcomer := db.get(User, newcomer.id)) is not None:
            delete_user(db, newcomer)


def test_another_member_cant_rename_it(api, member):
    with signed_in_as(member):
        resp = api.put("/api/organisation", json={"name": "Theirs"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only the organisation's creator can rename it."
    assert api.get("/api/organisation").json()["name"] == "Test User"


def test_a_name_another_organisation_has_is_refused(api, new_user):
    other = new_user()
    resp = api.put("/api/organisation", json={"name": other.full_name})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An organisation with that name already exists."
    assert api.get("/api/organisation").json()["name"] == "Test User"
    # Its own name again is fine.
    assert api.put("/api/organisation", json={"name": "Test User"}).status_code == 200
