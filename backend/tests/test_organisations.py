"""Organisations (`aios.organisations`): creating them, as `/api/me` lists them, and the
Organisation endpoints."""

import importlib.util
import threading
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import (
    build_user,
    delete_user,
    join_organisation,
    org_of,
    register,
)
from sqlalchemy import delete, select, text, update
from test_chats import signed_in_as

from aios import organisations
from aios.db import SessionLocal
from aios.models import (
    Agent,
    Chat,
    ChatVisibility,
    DataStore,
    DataStoreStatus,
    Organisation,
    OrganisationMember,
    User,
)

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


def backfill_before_slugs(db) -> None:
    """Run revision 0030's data step as it ran, before organisations had slugs (revision 0055),
    then give what it made slugs as 0055 does. In this test's transaction only."""
    with Operations.context(MigrationContext.configure(db.connection())):
        revision("0055_organisation_slugs").downgrade()
        revision("0030_personal_organisations").backfill(db.connection())
        revision("0055_organisation_slugs").upgrade()


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
        org = Organisation(
            name=f"made-{user_id}", slug=f"made-{user_id}", personal=True, created_by=user_id
        )
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

    backfill_before_slugs(db)

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
    assert (agent.updated_by, agent.archived_by) == (None, None)

    # Run again (a user already in an organisation), it makes no second one.
    backfill_before_slugs(db)
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


def test_creating_an_organisation_makes_the_person_its_creator_and_only_member(
    client, db, provisioning
):
    """From the welcome page: a person in no organisation creates one, which opens straight
    away, with its store queued to be provisioned at once."""
    person = registered(db, register(client, "Founder"))
    try:
        resp = client.post("/api/organisations", json={"name": "  Founders Ltd  "})
        assert resp.status_code == 201, resp.text
        org = resp.json()
        assert (org["name"], org["slug"], org["is_creator"]) == (
            "Founders Ltd",
            "founders-ltd",
            True,
        )
        assert org["creator"] == {"id": person.id, "name": "Founder Tester"}
        assert [m["user"]["id"] for m in org["members"]] == [person.id]
        assert client.get("/api/me").json()["organisations"] == [
            {"id": org["id"], "name": "Founders Ltd", "slug": "founders-ltd", "is_creator": True}
        ]
        # It opens: its routes answer under its prefix.
        assert client.get(f"/api/o/{org['id']}/organisation").json()["id"] == org["id"]
        stores = db.scalars(
            select(DataStore).where(DataStore.owner_organisation_id == org["id"])
        ).all()
        assert [s.status for s in stores] == [DataStoreStatus.provisioning]
        assert provisioning == [stores[0].id]
    finally:
        delete_user(db, person)


def test_a_member_creates_another_organisation_and_is_in_both(api, db, user):
    resp = api.post("/api/organisations", json={"name": "Side Project"})
    assert resp.status_code == 201, resp.text
    listed = [o["name"] for o in api.get("/api/me").json()["organisations"]]
    # The one joined most recently first.
    assert listed == ["Side Project", "Test User"]


def test_creating_an_organisation_with_a_taken_or_blank_name_is_refused(api, new_user, client):
    other = new_user()
    resp = api.post("/api/organisations", json={"name": other.full_name})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An organisation with that name already exists."
    assert api.post("/api/organisations", json={"name": " "}).status_code == 422
    assert [o["name"] for o in api.get("/api/me").json()["organisations"]] == ["Test User"]


def test_creating_an_organisation_needs_a_sign_in(client):
    assert client.post("/api/organisations", json={"name": "Nobody's"}).status_code == 401


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
    assert (org["name"], org["is_creator"]) == ("Test User", True)
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


def test_the_creator_renames_the_organisation(api, user):
    resp = api.put("/api/organisation", json={"name": "  Acme  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Acme"
    assert [o["name"] for o in api.get("/api/me").json()["organisations"]] == ["Acme"]
    assert api.put("/api/organisation", json={"name": " "}).status_code == 422


def test_renaming_to_a_name_a_new_organisation_is_taking_is_refused(api, db, user):
    """The creator renames to a name a new organisation is taking at the same moment: the rename
    is the one refused, as for a name already taken."""
    founder = build_user("acme")
    try:
        with SessionLocal() as creating:
            # The creation, flushed but not committed: the rename waits on its name.
            creating.add(founder)
            creating.flush()
            organisations.create(creating, founder, "Acme")
            renaming, result = in_the_background(
                lambda: api.put("/api/organisation", json={"name": "Acme"})
            )
            renaming.join(timeout=0.5)
            assert renaming.is_alive(), "the rename should wait on the new organisation's name"
            creating.commit()
        renaming.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 409
        assert result["value"].json()["detail"] == "An organisation with that name already exists."
        assert api.get("/api/organisation").json()["name"] == "Test User"
    finally:
        if (founder := db.get(User, founder.id)) is not None:
            delete_user(db, founder)


def test_two_organisations_made_at_once_whose_names_make_one_slug_get_distinct_ones(api, db, user):
    """The slug the second found free is taken by the time it inserts: it picks again."""
    founder = build_user("founder")
    try:
        with SessionLocal() as creating:
            creating.add(founder)
            creating.flush()
            organisations.create(creating, founder, "Clash Co")
            second, result = in_the_background(
                lambda: api.post("/api/organisations", json={"name": "clash co!"})
            )
            second.join(timeout=0.5)
            assert second.is_alive(), "the second should wait on the first's slug"
            creating.commit()
        second.join(timeout=10)

        assert "error" not in result, result
        assert result["value"].status_code == 201, result["value"].text
        made = result["value"].json()
        assert (made["name"], made["slug"]) == ("clash co!", "clash-co-2")
    finally:
        # The second is `user`'s, deleted with them.
        if (founder := db.get(User, founder.id)) is not None:
            delete_user(db, founder)


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


# Slugs: what a link in the browser names an organisation by (the API uses the id).


def created(api, name: str) -> dict:
    resp = api.post("/api/organisations", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_organisations_whose_names_make_the_same_slug_get_distinct_ones(api):
    """A slug is made from the name, and suffixed when another organisation has it."""
    made = [created(api, name) for name in ("Acme Slugs", "ACME slugs!", "acme  slugs")]
    assert [(o["name"], o["slug"]) for o in made] == [
        ("Acme Slugs", "acme-slugs"),
        ("ACME slugs!", "acme-slugs-2"),
        ("acme  slugs", "acme-slugs-3"),
    ]


def test_a_second_organisation_named_acme_gets_its_own_slug(db):
    """Names are unique for now, but slugs don't rely on it: a new organisation whose name
    another has gets the next free suffix."""
    try:
        db.add(Organisation(name="Acme", slug=organisations.free_slug(db, "Acme")))
        db.flush()
        assert organisations.free_slug(db, "Acme") == "acme-2"
        db.add(Organisation(name="acme!", slug=organisations.free_slug(db, "acme!")))
        db.flush()
        assert organisations.free_slug(db, "Acme") == "acme-3"
    finally:
        db.rollback()


def test_a_slug_is_made_of_plain_lower_case_words(api):
    slugs = [created(api, name)["slug"] for name in ("Zoë Ångström", "日本語", "-- __")]
    assert slugs[0] == "zoe-angstrom"
    # Nothing left of the name: suffixed after any other organisation's like it.
    assert all(s == "organisation" or s.startswith("organisation-") for s in slugs[1:])
    assert slugs[1] != slugs[2]


def test_renaming_an_organisation_leaves_its_slug_unchanged(api, user):
    [before] = api.get("/api/me").json()["organisations"]
    assert before["slug"] == "test-user"
    resp = api.put("/api/organisation", json={"name": "Acme"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "test-user"
    assert api.get("/api/me").json()["organisations"] == [{**before, "name": "Acme"}]


def test_the_upgrade_gives_every_organisation_a_slug_oldest_first(db):
    """Revision 0055 gives the organisations from before it theirs, as a new one would get
    it, in this test's transaction only."""
    with Operations.context(MigrationContext.configure(db.connection())):
        revision("0055_organisation_slugs").downgrade()
        ids = [
            db.execute(
                text("INSERT INTO organisations (name) VALUES (:name) RETURNING id"),
                {"name": name},
            ).scalar_one()
            for name in ("Upgraded Acme", "UPGRADED acme", "Zoë & Upgraded", "!!!")
        ]
        revision("0055_organisation_slugs").upgrade()
    slugs = db.execute(
        text("SELECT slug FROM organisations WHERE id = ANY(:ids) ORDER BY id"), {"ids": ids}
    ).scalars()
    made = list(slugs)
    assert made[:3] == ["upgraded-acme", "upgraded-acme-2", "zoe-upgraded"]
    # Nothing left of the name: suffixed after any other organisation's like it.
    assert made[3] == "organisation" or made[3].startswith("organisation-")


def test_me_lists_the_users_organisations_and_whether_they_created_each(api, db, user, member):
    expected = {"id": org_of(db, user).id, "name": "Test User", "slug": "test-user"}
    assert api.get("/api/me").json()["organisations"] == [{**expected, "is_creator": True}]
    with signed_in_as(member):
        assert api.get("/api/me").json()["organisations"] == [{**expected, "is_creator": False}]
