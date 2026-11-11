"""The development "sign in as" picker: `DEV_LOGIN_ENABLED` mounts `/api/dev/*`."""

import logging

import pytest
from conftest import delete_user, home_organisation, make_user
from fastapi.testclient import TestClient
from sqlalchemy import select

from aios.config import Settings, get_settings
from aios.main import create_app
from aios.models import User


def app_client(monkeypatch, enabled: bool) -> TestClient:
    monkeypatch.setattr(get_settings(), "dev_login_enabled", enabled)
    return TestClient(create_app())


@pytest.fixture
def dev_client(monkeypatch) -> TestClient:
    return app_client(monkeypatch, True)


@pytest.fixture
def users(db):
    """Users who sort apart from each other only by last name, then first name."""
    made = [
        make_user(db, "dev", first_name="Zed", last_name="Aardvark"),
        make_user(db, "dev", first_name="Bob", last_name="Bzzzt"),
        make_user(db, "dev", first_name="Alice", last_name="Bzzzt"),
    ]
    yield made
    for u in made:
        delete_user(db, u)


def test_with_the_setting_off_the_dev_routes_dont_exist(monkeypatch, users):
    client = app_client(monkeypatch, False)

    assert client.get("/api/dev/users").status_code == 404
    assert client.post("/api/dev/session", json={"user_id": users[0].id}).status_code == 404
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/auth/options").json() == {"dev_users": False}


def test_with_the_setting_on_the_options_offer_the_picker(dev_client):
    assert dev_client.get("/api/auth/options").json() == {"dev_users": True}


def test_the_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)
    assert Settings(_env_file=None).dev_login_enabled is False


def test_the_list_holds_every_user_by_last_name_then_first_name(dev_client, users, db):
    listed = dev_client.get("/api/dev/users").json()

    assert {u["id"] for u in listed} == set(db.scalars(select(User.id)))
    zed, bob, alice = users
    ours = [u for u in listed if u["id"] in {u.id for u in users}]
    assert ours == [
        {"id": u.id, "first_name": u.first_name, "last_name": u.last_name, "email": u.email}
        for u in (zed, alice, bob)
    ]


def test_picking_a_user_signs_in_as_them(dev_client, users, db, provisioning, caplog):
    chosen = users[1]

    with caplog.at_level(logging.INFO, logger="aios.dev_auth"):
        resp = dev_client.post("/api/dev/session", json={"user_id": chosen.id})

    org = home_organisation(db, chosen.id)
    expected = {
        "id": chosen.id,
        "first_name": chosen.first_name,
        "last_name": chosen.last_name,
        "email": chosen.email,
        "organisations": [
            {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "is_creator": True,
                "role": "admin",
                "deletes_at": None,
            }
        ],
    }
    assert resp.status_code == 200
    assert resp.json() == expected
    assert dev_client.get("/api/me").json() == expected
    # Nothing is made, as on any sign-in.
    assert provisioning == []
    assert any(chosen.email in r.getMessage() for r in caplog.records)


def test_picking_a_user_who_doesnt_exist_is_a_404(dev_client):
    resp = dev_client.post("/api/dev/session", json={"user_id": 2**31 - 1})

    assert resp.status_code == 404
    assert dev_client.get("/api/me").status_code == 401


def test_starting_the_app_with_the_picker_on_logs_a_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        app_client(monkeypatch, True)

    assert any(
        r.levelno == logging.WARNING and "DEV_LOGIN_ENABLED" in r.getMessage()
        for r in caplog.records
    )
