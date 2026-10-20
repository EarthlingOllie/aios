"""Rate limits on signing in and registering, and the cross-site guard on the auth endpoints."""

import secrets

import pytest
from conftest import TEST_PASSWORD, delete_user, signed_in_client
from fastapi.testclient import TestClient
from sqlalchemy import select

from aios.config import get_settings
from aios.main import app
from aios.models import User

TOO_MANY = {"detail": "Too many attempts. Try again later."}


def sign_in(client: TestClient, email: str, password: str = "the wrong password", **headers):
    return client.post(
        "/api/auth/login", json={"email": email, "password": password}, headers=headers
    )


@pytest.fixture
def registered(db):
    """Deletes the users a test registers (by the emails it names), afterwards."""
    emails: list[str] = []
    yield emails
    for email in emails:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            delete_user(db, user)


def register(client: TestClient, registered: list[str], **headers):
    email = f"new-{secrets.token_hex(4)}@example.com"
    registered.append(email)
    body = {
        "first_name": "Alice",
        "last_name": "Smith",
        "email": email,
        "password": "a long passphrase",
        "password_confirm": "a long passphrase",
    }
    return client.post("/api/auth/register", json=body, headers=headers)


def assert_throttled(resp) -> None:
    assert resp.status_code == 429
    assert resp.json() == TOO_MANY
    assert 0 < int(resp.headers["Retry-After"]) <= 15 * 60


def test_signing_in_past_the_limit_for_an_email_is_throttled(monkeypatch, user):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_email", "3/15 minutes")
    client = TestClient(app)
    for _ in range(3):
        assert sign_in(client, user.email).status_code == 401

    # Even with the right password, and with the email written another way.
    assert_throttled(sign_in(client, f" {user.email.upper()}", TEST_PASSWORD))
    assert client.get("/api/me").status_code == 401
    # Another email isn't.
    assert sign_in(client, "someone-else@example.com").status_code == 401


def test_signing_in_past_the_limit_for_an_ip_is_throttled(monkeypatch, user):
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_ip", "3/15 minutes")
    client = TestClient(app)
    for i in range(3):
        assert sign_in(client, f"nobody-{i}@example.com").status_code == 401

    assert_throttled(sign_in(client, user.email, TEST_PASSWORD))


def test_registering_past_the_limit_for_an_ip_is_throttled(monkeypatch, db, registered):
    monkeypatch.setattr(get_settings(), "register_rate_limit_per_ip", "2/hour")
    client = TestClient(app)
    for _ in range(2):
        assert register(client, registered).status_code == 201

    resp = register(client, registered)

    assert resp.status_code == 429
    assert resp.json() == TOO_MANY
    assert 0 < int(resp.headers["Retry-After"]) <= 60 * 60
    assert db.scalar(select(User).where(User.email == registered[-1])) is None


def test_clients_behind_the_load_balancer_are_counted_by_their_own_ip(monkeypatch, registered):
    monkeypatch.setattr(get_settings(), "register_rate_limit_per_ip", "1/hour")
    monkeypatch.setattr(get_settings(), "trusted_proxy_count", 1)
    client = TestClient(app)
    alice = {"X-Forwarded-For": "203.0.113.1"}
    # The balancer appends the address it saw, after anything the client sent.
    bob = {"X-Forwarded-For": "203.0.113.1, 203.0.113.2"}

    assert register(client, registered, **alice).status_code == 201
    assert register(client, registered, **bob).status_code == 201
    assert register(client, registered, **alice).status_code == 429
    assert register(client, registered, **bob).status_code == 429


def test_without_a_proxy_a_forwarded_address_is_ignored(monkeypatch, registered):
    monkeypatch.setattr(get_settings(), "register_rate_limit_per_ip", "1/hour")
    client = TestClient(app)

    assert register(client, registered, **{"X-Forwarded-For": "203.0.113.1"}).status_code == 201
    resp = register(client, registered, **{"X-Forwarded-For": "203.0.113.2"})
    assert resp.status_code == 429


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:5173.evil"])
def test_a_cross_site_sign_in_is_refused(monkeypatch, user, origin):
    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:5173")
    client = TestClient(app)

    resp = sign_in(client, user.email, TEST_PASSWORD, Origin=origin)

    assert resp.status_code == 403
    assert client.get("/api/me").status_code == 401


def test_a_cross_site_registration_and_sign_out_are_refused(monkeypatch, user, registered):
    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:5173")
    evil = {"Origin": "https://evil.example"}

    assert register(TestClient(app), registered, **evil).status_code == 403
    client = signed_in_client(user)
    assert client.post("/api/auth/logout", headers=evil).status_code == 403
    assert client.get("/api/me").status_code == 200


def test_a_same_origin_sign_in_is_accepted(monkeypatch, user):
    monkeypatch.setattr(get_settings(), "public_url", "https://aios.example/")
    client = TestClient(app)

    resp = sign_in(client, user.email, TEST_PASSWORD, Origin="https://aios.example")

    assert resp.status_code == 200
    assert client.get("/api/me").status_code == 200


def test_a_sign_in_that_isnt_json_is_refused(user):
    # As a cross-site form could send it: a plain-text body holding JSON.
    client = TestClient(app)
    body = f'{{"email": "{user.email}", "password": "{TEST_PASSWORD}"}}'

    resp = client.post("/api/auth/login", content=body, headers={"Content-Type": "text/plain"})

    assert resp.status_code == 422
    assert client.get("/api/me").status_code == 401
