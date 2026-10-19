"""Organisations: every user is in one (`aios.organisations`), shown by `/api/me` and the
Organisation endpoints."""

import importlib.util
import threading
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select, update
from test_chats import signed_in_as

from aios import organisations
from aios.db import SessionLocal
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


def test_the_upgrade_gives_each_user_a_personal_organisation_and_stamps_what_they_own(db):
    """The data step (revision 0030), on users made before organisations existed: two whose
    logins would name the same organisation, and one whose login is the suffixed name."""
    first, second, third = (
        User(github_id=-901, login="sam"),
        # A login is unique on GitHub at a time, but a user renamed since keeps their old one here.
        User(github_id=-902, login="sam"),
        User(github_id=-903, login="sam-2"),
    )
    db.add_all([first, second, third])
    db.flush()
    # What they own, as it was before organisations: made in an organisation of theirs, which
    # the contract's downgrade makes theirs to own, and the organisation then undone.
    made = [organisations.create_personal(db, u) for u in (first, second)]
    agent = Agent(organisation_id=made[1].id, created_by=second.id, name="Reviewer")
    chat = Chat(
        organisation_id=made[0].id,
        created_by=first.id,
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

    def organisation_of(user: User) -> Organisation:
        return db.scalar(
            select(Organisation)
            .join(OrganisationMember)
            .where(OrganisationMember.user_id == user.id)
        )

    # Named after the login, suffixed on a clash; in the order the users signed up.
    names = [organisation_of(u).name for u in (first, second, third)]
    assert names == ["sam", "sam-2", "sam-2-2"]
    for u in (first, second, third):
        org = organisation_of(u)
        assert (org.personal, org.created_by) == (True, u.id)
        assert [m.user_id for m in org.members] == [u.id]

    db.refresh(agent)
    db.refresh(chat)
    assert (agent.organisation_id, agent.created_by) == (organisation_of(second).id, second.id)
    assert (chat.organisation_id, chat.created_by) == (organisation_of(first).id, first.id)
    # Nothing else is claimed: who last changed or deleted it isn't known.
    assert (agent.updated_by, agent.deleted_by) == (None, None)

    # Run again (a user already in an organisation), it makes no second one.
    revision("0030_personal_organisations").backfill(db.connection())
    assert (
        db.scalar(
            select(OrganisationMember.organisation_id).where(OrganisationMember.user_id == first.id)
        )
        == organisation_of(first).id
    )
    assert db.scalar(select(Organisation).where(Organisation.name == "sam-3")) is None


def sign_in(client, monkeypatch, github_id: int, login: str):
    """Drive the OAuth callback as `test_auth` does, as this GitHub user; returns the response."""
    from aios import auth

    class Response:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    async def authorize_access_token(request):
        return {"access_token": "t"}

    async def get(path, token):
        return Response({"id": github_id, "login": login, "email": f"{login}@example.com"})

    monkeypatch.setattr(auth.oauth.github, "authorize_access_token", authorize_access_token)
    monkeypatch.setattr(auth.oauth.github, "get", get)
    return client.get("/auth/callback?code=x&state=y", follow_redirects=False)


def test_signing_in_for_the_first_time_makes_a_personal_organisation_once(client, db, monkeypatch):
    from conftest import delete_user

    try:
        assert sign_in(client, monkeypatch, -43, "newcomer").status_code == 303
        user = db.scalar(select(User).where(User.github_id == -43))
        [org] = db.scalars(
            select(Organisation)
            .join(OrganisationMember)
            .where(OrganisationMember.user_id == user.id)
        ).all()
        assert (org.name, org.personal, org.created_by) == ("newcomer", True, user.id)
        assert [m.user_id for m in org.members] == [user.id]

        # Signing in again finds the same one.
        assert sign_in(client, monkeypatch, -43, "newcomer").status_code == 303
        db.expire_all()
        named = select(Organisation).where(Organisation.name.like("newcomer%"))
        assert db.scalars(named).all() == [org]
    finally:
        user = db.scalar(select(User).where(User.github_id == -43))
        if user is not None:
            delete_user(db, user)


def test_a_personal_organisation_named_after_a_taken_login_is_suffixed(client, db, monkeypatch):
    from conftest import delete_user

    try:
        sign_in(client, monkeypatch, -44, "twin")
        # Another GitHub account with the same login (the first renamed since, say).
        sign_in(client, monkeypatch, -45, "twin")
        names = db.scalars(
            select(Organisation.name)
            .join(OrganisationMember)
            .join(User, User.id == OrganisationMember.user_id)
            .where(User.github_id.in_([-44, -45]))
            .order_by(User.github_id.desc())
        ).all()
        assert names == ["twin", "twin-2"]
    finally:
        for u in db.scalars(select(User).where(User.github_id.in_([-44, -45]))).all():
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


def test_signing_in_while_a_rename_takes_the_login_lands_in_a_suffixed_one(
    client, db, user, monkeypatch
):
    """The creator of another organisation renames it to the login as the user first signs in:
    the name looked free to the sign-in, and is taken by the time it inserts."""
    from conftest import delete_user

    try:
        with SessionLocal() as renaming:
            # The rename, flushed but not committed: the sign-in waits on its name.
            organisations.membership_of(renaming, user.id).name = "dana"
            renaming.flush()
            signing_in, result = in_the_background(
                lambda: sign_in(client, monkeypatch, -46, "dana")
            )
            signing_in.join(timeout=0.5)
            assert signing_in.is_alive(), "the sign-in should wait on the rename's name"
            renaming.commit()
        signing_in.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 303
        dana = db.scalar(select(User).where(User.github_id == -46))
        org = organisations.membership_of(db, dana.id)
        assert (org.name, org.personal, org.created_by) == ("dana-2", True, dana.id)
    finally:
        dana = db.scalar(select(User).where(User.github_id == -46))
        if dana is not None:
            delete_user(db, dana)


def test_me_says_which_organisation_the_user_is_in(api, user):
    me = api.get("/api/me").json()
    assert me["login"] == "test-user"
    assert me["organisation"] == {
        "id": me["organisation"]["id"],
        "name": "test-user",
        "personal": True,
        "is_creator": True,
    }


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
    db.delete(u)
    db.commit()


def test_the_organisation_page_shows_the_name_creator_and_members_with_when_they_joined(
    api, user, member
):
    org = api.get("/api/organisation").json()
    assert (org["name"], org["personal"], org["is_creator"]) == ("test-user", True, True)
    assert org["creator"] == {
        "id": user.id,
        "login": "test-user",
        "name": None,
        "avatar_url": None,
    }
    assert [(m["user"]["login"], m["user"]["name"], m["invite_id"]) for m in org["members"]] == [
        ("test-user", None, None),
        ("member", "Member Person", None),
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
    from conftest import delete_user

    try:
        with SessionLocal() as signing_up:
            # The sign-up, flushed but not committed: the rename waits on its name.
            newcomer = User(github_id=-48, login="acme")
            signing_up.add(newcomer)
            signing_up.flush()
            organisations.create_personal(signing_up, newcomer)
            renaming, result = in_the_background(
                lambda: api.put("/api/organisation", json={"name": "acme"})
            )
            renaming.join(timeout=0.5)
            assert renaming.is_alive(), "the rename should wait on the sign-up's name"
            signing_up.commit()
        renaming.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 409
        assert result["value"].json()["detail"] == "An organisation with that name already exists."
        assert api.get("/api/organisation").json()["name"] == "test-user"
    finally:
        newcomer = db.scalar(select(User).where(User.github_id == -48))
        if newcomer is not None:
            delete_user(db, newcomer)


def test_another_member_cant_rename_it(api, member):
    with signed_in_as(member):
        resp = api.put("/api/organisation", json={"name": "Theirs"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only the organisation's creator can rename it."
    assert api.get("/api/organisation").json()["name"] == "test-user"


def test_a_name_another_organisation_has_is_refused(api, new_user):
    other = new_user()
    resp = api.put("/api/organisation", json={"name": other.login})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An organisation with that name already exists."
    assert api.get("/api/organisation").json()["name"] == "test-user"
    # Its own name again is fine.
    assert api.put("/api/organisation", json={"name": "test-user"}).status_code == 200
