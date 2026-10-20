import secrets

import pytest
from conftest import TEST_PASSWORD, delete_user, signed_in_client
from fastapi.testclient import TestClient
from sqlalchemy import select

from aios.main import app
from aios.models import DataStore, DataStoreStatus, User


def test_me_requires_session(client):
    assert client.get("/api/me").status_code == 401


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.fixture
def registered(db):
    """Deletes the users a test registers (by the emails it names), afterwards."""
    emails: list[str] = []
    yield emails
    for email in emails:
        user = db.scalar(select(User).where(User.email == email.lower()))
        if user is not None:
            delete_user(db, user)


def new_email() -> str:
    return f"new-{secrets.token_hex(4)}@example.com"


def registration(email: str, **values) -> dict:
    return {
        "first_name": "Alice",
        "last_name": "Smith",
        "email": email,
        "password": "a long passphrase",
        "password_confirm": "a long passphrase",
        **values,
    }


def test_registering_signs_the_new_user_in(client, registered):
    email = new_email()
    registered.append(email)

    resp = client.post("/api/auth/register", json=registration(email))

    assert resp.status_code == 201
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json() == resp.json()
    assert me.json() == {
        "id": me.json()["id"],
        "first_name": "Alice",
        "last_name": "Smith",
        "email": email,
    }


def test_registering_queues_the_new_users_data_store(client, db, registered, provisioning):
    email = new_email()
    registered.append(email)

    resp = client.post("/api/auth/register", json=registration(email))

    user_id = resp.json()["id"]
    stores = db.scalars(select(DataStore).where(DataStore.owner_user_id == user_id)).all()
    assert [s.status for s in stores] == [DataStoreStatus.provisioning]
    assert provisioning == [stores[0].id]


def test_registering_trims_names_and_email_and_stores_the_email_normalised(client, db, registered):
    email = new_email()
    registered.append(email)

    resp = client.post(
        "/api/auth/register",
        json=registration(f"  {email.upper()} ", first_name=" Alice ", last_name="\tSmith "),
    )

    assert resp.status_code == 201
    assert client.get("/api/me").json() | {"id": 0} == {
        "id": 0,
        "first_name": "Alice",
        "last_name": "Smith",
        "email": email,
    }
    user = db.scalar(select(User).where(User.email == email))
    assert user.password_hash != "a long passphrase"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"password_confirm": "another passphrase"}, "password_confirm"),
        ({"password": "short", "password_confirm": "short"}, "password"),
        ({"password": "x" * 257, "password_confirm": "x" * 257}, "password"),
        ({"first_name": ""}, "first_name"),
        ({"last_name": "   "}, "last_name"),
        ({"first_name": "x" * 101}, "first_name"),
        ({"email": "not an email"}, "email"),
        ({"email": "alice@"}, "email"),
    ],
)
def test_registration_refuses_bad_details_next_to_their_field(client, registered, change, field):
    email = new_email()
    registered.append(email)

    resp = client.post("/api/auth/register", json=registration(email) | change)

    assert resp.status_code == 422
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", field]]
    assert client.get("/api/me").status_code == 401


def test_passwords_of_12_and_256_characters_are_accepted(client, registered):
    for password in ("x" * 12, "x" * 256):
        email = new_email()
        registered.append(email)
        body = registration(email, password=password, password_confirm=password)
        assert client.post("/api/auth/register", json=body).status_code == 201


def test_mismatched_passwords_say_so(client, registered):
    email = new_email()
    registered.append(email)

    resp = client.post(
        "/api/auth/register", json=registration(email, password_confirm="another passphrase")
    )

    assert resp.json()["detail"][0]["msg"] == "Passwords don't match"


def test_an_email_already_registered_in_any_case_is_refused(client, registered):
    email = new_email()
    registered.append(email)
    assert client.post("/api/auth/register", json=registration(email)).status_code == 201

    other = TestClient(app)
    resp = other.post("/api/auth/register", json=registration(email.upper(), first_name="Eve"))

    assert resp.status_code == 409
    assert other.get("/api/me").status_code == 401


def test_signing_in_with_the_email_in_any_case(db, user):
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login", json={"email": f" {user.email.upper()}", "password": TEST_PASSWORD}
    )

    assert resp.status_code == 200
    assert resp.json()["id"] == user.id
    assert client.get("/api/me").json()["email"] == user.email


def test_signing_in_starts_the_users_data_store_if_they_have_none(db, new_user, provisioning):
    someone = new_user()
    client = TestClient(app)

    client.post("/api/auth/login", json={"email": someone.email, "password": TEST_PASSWORD})

    store = db.scalar(select(DataStore).where(DataStore.owner_user_id == someone.id))
    assert provisioning == [store.id]


@pytest.mark.parametrize(
    "credentials",
    [
        {"password": "the wrong password"},
        {"email": "nobody@example.com"},
        {"email": "not an email"},
        {"password": "x" * 1000},
    ],
)
def test_a_wrong_password_and_an_unknown_email_are_refused_alike(user, credentials):
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD, **credentials}
    )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Email or password is incorrect"}
    assert client.get("/api/me").status_code == 401


def test_signing_out_ends_the_session(user):
    client = TestClient(app)
    client.post("/api/auth/login", json={"email": user.email, "password": TEST_PASSWORD})
    assert client.get("/api/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 204

    assert client.get("/api/me").status_code == 401


def test_a_session_from_another_session_version_is_signed_out(db, user):
    client = signed_in_client(user)
    assert client.get("/api/me").status_code == 200

    user.session_version += 1
    db.commit()

    assert client.get("/api/me").status_code == 401


def test_the_options_offer_no_dev_users_yet(client):
    assert client.get("/api/auth/options").json() == {"dev_users": False}


def test_the_github_routes_are_gone(client):
    assert client.get("/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback?code=x&state=y").status_code == 404
    assert client.post("/logout").status_code == 404
