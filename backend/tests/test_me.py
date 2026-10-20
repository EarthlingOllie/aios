"""The signed-in user's own account: `/api/me`."""

import secrets

import pytest
from conftest import TEST_PASSWORD, delete_user, make_user, signed_in_client
from fastapi.testclient import TestClient

from aios.config import get_settings
from aios.main import app


def test_editing_both_names(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"first_name": "Alice", "last_name": "Smith"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": user.id,
        "first_name": "Alice",
        "last_name": "Smith",
        "email": user.email,
    }
    assert client.get("/api/me").json() == resp.json()


@pytest.mark.parametrize(
    ("change", "after"),
    [
        ({"first_name": "Alice"}, ("Alice", "User")),
        ({"last_name": "Smith"}, ("Test", "Smith")),
        ({}, ("Test", "User")),
    ],
)
def test_a_name_left_out_is_kept(user, change, after):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json=change)
    assert resp.status_code == 200, resp.text
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == after


def test_names_are_trimmed(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"first_name": "  Alice ", "last_name": " Smith  "})
    assert resp.status_code == 200, resp.text
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Alice", "Smith")


@pytest.mark.parametrize("field", ["first_name", "last_name"])
@pytest.mark.parametrize("value", ["", "   ", "x" * 101, None])
def test_a_bad_name_is_refused_next_to_its_field(user, field, value):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={field: value})
    assert resp.status_code == 422, resp.text
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", field]]
    me = client.get("/api/me").json()
    assert (me["first_name"], me["last_name"]) == ("Test", "User")


def test_a_name_of_100_characters_is_accepted(user):
    client = signed_in_client(user)
    resp = client.patch("/api/me", json={"last_name": "x" * 100})
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_name"] == "x" * 100


def test_editing_needs_a_session(client):
    assert client.patch("/api/me", json={"first_name": "Alice"}).status_code == 401


def test_editing_keeps_the_session(user):
    client = signed_in_client(user)
    assert client.patch("/api/me", json={"first_name": "Alice"}).status_code == 200
    other = signed_in_client(user)
    assert other.get("/api/me").status_code == 200


def test_chats_show_the_new_name(user, files_bucket):
    client = signed_in_client(user)
    chat = client.post("/api/chats", json={"name": "Renames"}).json()
    resp = client.post(f"/api/chats/{chat['id']}/messages", json={"body": "Hi"})
    assert resp.status_code == 201, resp.text

    resp = client.patch("/api/me", json={"first_name": "Alice", "last_name": "Smith"})
    assert resp.status_code == 200, resp.text

    members = client.get(f"/api/chats/{chat['id']}").json()["members"]["users"]
    assert [m["name"] for m in members] == ["Alice Smith"]
    [message] = client.get(f"/api/chats/{chat['id']}/messages").json()["messages"]
    assert message["author"]["name"] == "Alice Smith"


def sign_in(email: str, password: str = TEST_PASSWORD):
    client = TestClient(app)
    return client.post("/api/auth/login", json={"email": email, "password": password})


def change_email(client: TestClient, email: str, current_password: str = TEST_PASSWORD, **headers):
    body = {"email": email, "current_password": current_password}
    return client.put("/api/me/email", json=body, headers=headers)


def test_changing_the_email_moves_sign_in_to_the_new_one(user):
    old_email = user.email
    new_email = f"renamed-{secrets.token_hex(4)}@example.com"
    client = signed_in_client(user)

    resp = change_email(client, new_email)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": user.id,
        "first_name": "Test",
        "last_name": "User",
        "email": new_email,
    }
    assert client.get("/api/me").json()["email"] == new_email
    assert sign_in(new_email).status_code == 200
    assert sign_in(old_email).status_code == 401


def test_changing_the_email_needs_the_current_password(user):
    old_email = user.email
    client = signed_in_client(user)

    resp = change_email(client, "someone-new@example.com", "not my password")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Current password is incorrect"}
    assert client.get("/api/me").json()["email"] == old_email
    assert sign_in(old_email).status_code == 200


@pytest.mark.parametrize("written", [str, str.upper, lambda e: f"  {e.title()} "])
def test_an_email_thats_taken_in_any_case_is_refused(db, user, written):
    other = make_user(db, "other")
    try:
        client = signed_in_client(user)

        resp = change_email(client, written(other.email))

        assert resp.status_code == 409
        assert resp.json() == {"detail": "An account with this email already exists"}
        assert client.get("/api/me").json()["email"] == user.email
    finally:
        delete_user(db, other)


def test_the_new_email_is_normalised(user):
    client = signed_in_client(user)
    new_email = f"renamed-{secrets.token_hex(4)}@example.com"

    resp = change_email(client, f"  {new_email.upper()} ")

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == new_email
    assert sign_in(new_email).status_code == 200


def test_keeping_the_same_email_is_accepted(user):
    resp = change_email(signed_in_client(user), user.email.upper())
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == user.email


@pytest.mark.parametrize("email", ["", "not an email", "alice@", "@example.com"])
def test_an_invalid_email_is_refused_next_to_its_field(user, email):
    client = signed_in_client(user)

    resp = change_email(client, email)

    assert resp.status_code == 422, resp.text
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", "email"]]
    assert client.get("/api/me").json()["email"] == user.email


def test_changing_the_email_keeps_every_session(user):
    client = signed_in_client(user)
    other = signed_in_client(user)

    assert change_email(client, f"renamed-{secrets.token_hex(4)}@example.com").status_code == 200

    assert client.get("/api/me").status_code == 200
    assert other.get("/api/me").status_code == 200


def test_changing_the_email_needs_a_session(client):
    assert change_email(client, "someone@example.com").status_code == 401


NEW_PASSWORD = "a brand new passphrase"


def change_password(
    client: TestClient,
    new_password: str = NEW_PASSWORD,
    current_password: str = TEST_PASSWORD,
    new_password_confirm: str | None = None,
    **headers,
):
    body = {
        "current_password": current_password,
        "new_password": new_password,
        "new_password_confirm": new_password
        if new_password_confirm is None
        else new_password_confirm,
    }
    return client.put("/api/me/password", json=body, headers=headers)


def test_changing_the_password_signs_out_every_other_session(user):
    client = signed_in_client(user)
    other = signed_in_client(user)

    resp = change_password(client)

    assert resp.status_code == 204, resp.text
    signed_out = other.get("/api/me")
    assert signed_out.status_code == 401
    # As the app tells an ended session apart from a wrong password.
    assert signed_out.json() == {"detail": "Not signed in"}
    assert client.get("/api/me").status_code == 200
    assert sign_in(user.email, NEW_PASSWORD).status_code == 200
    assert sign_in(user.email, TEST_PASSWORD).status_code == 401


def test_changing_the_password_needs_the_current_password(user):
    client = signed_in_client(user)
    other = signed_in_client(user)

    resp = change_password(client, current_password="not my password")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Current password is incorrect"}
    assert client.get("/api/me").status_code == 200
    assert other.get("/api/me").status_code == 200
    assert sign_in(user.email, TEST_PASSWORD).status_code == 200
    assert sign_in(user.email, NEW_PASSWORD).status_code == 401


@pytest.mark.parametrize(
    ("new_password", "confirm", "field", "message"),
    [
        ("x" * 11, None, "new_password", "Password must be at least 12 characters"),
        ("x" * 257, None, "new_password", "Password must be at most 256 characters"),
        (NEW_PASSWORD, "a different passphrase", "new_password_confirm", "Passwords don't match"),
    ],
    ids=["too-short", "too-long", "mismatched"],
)
def test_a_bad_new_password_is_refused_next_to_its_field(
    user, new_password, confirm, field, message
):
    client = signed_in_client(user)

    resp = change_password(client, new_password, new_password_confirm=confirm)

    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    assert [(e["loc"], e["msg"]) for e in errors] == [(["body", field], message)]
    assert client.get("/api/me").status_code == 200
    assert sign_in(user.email, TEST_PASSWORD).status_code == 200


@pytest.mark.parametrize("length", [12, 256])
def test_a_new_password_at_the_length_limits_is_accepted(user, length):
    resp = change_password(signed_in_client(user), "x" * length)
    assert resp.status_code == 204, resp.text
    assert sign_in(user.email, "x" * length).status_code == 200


def test_changing_the_password_needs_a_session(client):
    assert change_password(client).status_code == 401


def assert_throttled(resp) -> None:
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too many attempts. Try again later."}
    assert 0 < int(resp.headers["Retry-After"]) <= 15 * 60


def test_current_password_checks_past_the_limit_are_throttled(monkeypatch, db, user):
    monkeypatch.setattr(get_settings(), "current_password_rate_limit_per_user", "3/15 minutes")
    client = signed_in_client(user)
    # The two forms share the user's count, across sessions.
    assert change_email(client, "someone-new@example.com", "wrong").status_code == 401
    assert change_password(client, current_password="wrong").status_code == 401
    assert change_password(signed_in_client(user), current_password="wrong").status_code == 401

    # Even with the right password.
    assert_throttled(change_password(client))
    assert_throttled(change_email(client, "someone-new@example.com"))
    assert sign_in(user.email, TEST_PASSWORD).status_code == 200
    assert client.get("/api/me").json()["email"] == user.email

    # Another user isn't.
    other = make_user(db, "other")
    try:
        assert change_password(signed_in_client(other), current_password="wrong").status_code == 401
    finally:
        delete_user(db, other)


def test_cross_site_email_and_password_changes_are_refused(monkeypatch, user):
    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:5173")
    evil = {"Origin": "https://evil.example"}
    client = signed_in_client(user)

    assert change_email(client, "someone-new@example.com", **evil).status_code == 403
    assert change_password(client, **evil).status_code == 403

    assert client.get("/api/me").json()["email"] == user.email
    assert sign_in(user.email, TEST_PASSWORD).status_code == 200
