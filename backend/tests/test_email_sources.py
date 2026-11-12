"""Email sources (`aios.push_sources`): an address on the deployment's inbound mail domain,
made through the sources API; mail arrives as the receiving service posts it
(`aios.api.inbound_mail`). Outcomes: what's stored, threaded, searchable and counted."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_sync import INJECTION, found, items, offline, run, source_of, store_id  # noqa: F401

from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Document, Event
from aios.jev import Verdict
from aios.main import app

DOMAIN = "in.aios.test"
TOKEN = "inbound-test-token"


@pytest.fixture(autouse=True)
def inbound(monkeypatch):
    monkeypatch.setattr(get_settings(), "inbound_mail_domain", DOMAIN)
    monkeypatch.setattr(
        get_settings(), "inbound_mail_secret", type(get_settings().inbound_mail_secret)(TOKEN)
    )


@pytest.fixture
def mailer() -> TestClient:
    """The receiving mail service: no session."""
    return TestClient(app)


def add(api, senders=("alice@example.com",)) -> dict:
    resp = api.post(
        "/api/sources", json={"name": "Inbox", "kind": "email", "allowed_senders": list(senders)}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def mail(mailer, to: str, *, token: str = TOKEN, **fields):
    body = {
        "to": [to],
        "from": "Alice <alice@example.com>",
        "subject": "Quarterly figures",
        "message_id": "<m1@example.com>",
        "text": "Revenue rose by a tenth.",
        "spf": "pass",
        "dkim": "pass",
        **fields,
    }
    return mailer.post("/api/inbound-mail", json=body, headers={"Authorization": f"Bearer {token}"})


def attachment(name: str, data: bytes, content_type: str = "text/markdown") -> dict:
    return {
        "filename": name,
        "content_type": content_type,
        "content": base64.b64encode(data).decode(),
    }


def last_sync(api, source_id) -> dict:
    return source_of(api, source_id)["syncs"][0]


def test_an_email_source_gives_an_unguessable_address_once(api):
    made = add(api)
    address = made["email_address"]
    local, _, domain = address.partition("@")
    assert domain == DOMAIN and len(local) >= 32
    assert made["allowed_senders"] == ["alice@example.com"]
    shown = source_of(api, made["id"])
    assert shown["email_address"] is None
    assert address not in api.get("/api/sources").text
    assert add(api)["email_address"] != address
    assert api.post(f"/api/sources/{made['id']}/sync").status_code == 409


def test_mail_from_an_allowed_sender_is_stored_and_searchable(api, mailer, store_id):
    made = add(api)
    resp = mail(mailer, made["email_address"])
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"reached": 1}
    stored = items(store_id, made["id"])
    assert stored["m1@example.com"].verdict == Verdict.clean
    assert stored["m1@example.com"].title == "Quarterly figures"
    [hit] = found(store_id, "revenue")
    assert "Revenue rose by a tenth." in hit
    assert last_sync(api, made["id"])["added"] == 1


def test_an_email_source_with_no_allowed_senders_accepts_nothing(api, mailer, store_id):
    made = add(api, senders=())
    assert made["allowed_senders"] == []
    assert mail(mailer, made["email_address"]).status_code == 202
    assert items(store_id, made["id"]) == {}
    sync = last_sync(api, made["id"])
    assert sync["dropped"] == 1
    assert "no senders" in sync["error"]


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"from": "Mallory <mallory@evil.example>"}, "isn't allowed"),
        ({"spf": "fail", "dkim": "fail"}, "sender authentication"),
        ({"spf": None, "dkim": "none"}, "sender authentication"),
    ],
)
def test_spoofed_or_unauthenticated_mail_is_dropped_and_counted(api, mailer, store_id, fields, why):
    made = add(api)
    assert mail(mailer, made["email_address"], **fields).status_code == 202
    assert items(store_id, made["id"]) == {}
    sync = last_sync(api, made["id"])
    assert sync["dropped"] == 1
    assert why in sync["error"]


def test_a_whole_domain_can_be_allowed(api, mailer, store_id):
    made = add(api, senders=["@Example.com"])
    assert made["allowed_senders"] == ["@example.com"]
    mail(mailer, made["email_address"], **{"from": "bob@example.com"})
    assert len(items(store_id, made["id"])) == 1
    assert (
        api.post(
            "/api/sources",
            json={"name": "Bad", "kind": "email", "allowed_senders": ["not an address"]},
        ).status_code
        == 422
    )


def test_replies_thread_together(api, mailer, store_id):
    made = add(api)
    to = made["email_address"]
    mail(mailer, to, message_id="<a@example.com>", subject="Launch")
    mail(
        mailer,
        to,
        message_id="<b@example.com>",
        subject="Re: Launch",
        in_reply_to="<a@example.com>",
    )
    mail(
        mailer,
        to,
        message_id="<c@example.com>",
        subject="Re: Re: Launch",
        in_reply_to="<b@example.com>",
        references=["<a@example.com>", "<b@example.com>"],
    )
    mail(mailer, to, message_id="<d@example.com>", subject="Other")

    stored = items(store_id, made["id"])
    root = stored["a@example.com"]
    assert root.parent_id is None
    assert stored["b@example.com"].parent_id == root.id
    assert stored["c@example.com"].parent_id == root.id
    assert stored["d@example.com"].parent_id is None


def test_attachments_are_ingested_as_documents_within_the_size_cap(
    api, mailer, store_id, monkeypatch
):
    monkeypatch.setattr(get_settings(), "email_attachment_max_bytes", 40)
    made = add(api)
    mail(
        mailer,
        made["email_address"],
        attachments=[
            attachment("figures.md", b"Margins held at a fifth."),
            attachment("huge.md", b"x" * 41),
        ],
    )
    stored = items(store_id, made["id"])
    message = stored["m1@example.com"]
    [file] = [i for i in stored.values() if i.kind == "file"]
    assert (file.title, file.parent_id) == ("figures.md", message.id)
    with store_session(store_id, "ro") as store:
        [doc] = store.scalars(select(Document).where(Document.source_id == made["id"])).all()
        assert doc.filename == "figures.md"
        ingested = [
            e.payload for e in store.scalars(select(Event)) if e.type == "document_ingested"
        ]
    assert found(store_id, "margins") == ["Margins held at a fifth."]
    assert [(e["name"], e["source_id"]) for e in ingested] == [("figures.md", made["id"])]
    sync = last_sync(api, made["id"])
    assert (sync["added"], sync["dropped"]) == (2, 1)


def test_rotating_the_address_cuts_off_the_old_one(api, mailer, store_id):
    made = add(api)
    old = made["email_address"]
    resp = api.post(f"/api/sources/{made['id']}/secret")
    assert resp.status_code == 200, resp.text
    new = resp.json()["email_address"]
    assert new not in (None, old)

    assert mail(mailer, old).json() == {"reached": 0}
    assert items(store_id, made["id"]) == {}
    assert mail(mailer, new).json() == {"reached": 1}
    assert len(items(store_id, made["id"])) == 1


def test_a_flagged_email_isnt_searchable_and_one_awaiting_check_becomes_searchable(
    api, mailer, store_id, jev_classifier
):
    from aios import verdicts

    made = add(api)
    to = made["email_address"]
    mail(mailer, to, message_id="<bad@x>", text=f"Mangoes. {INJECTION}")
    assert items(store_id, made["id"])["bad@x"].verdict == Verdict.flagged
    assert found(store_id, "mangoes") == []

    jev_classifier.down = True
    mail(mailer, to, message_id="<late@x>", text="Lychees are in season.")
    assert items(store_id, made["id"])["late@x"].verdict == Verdict.awaiting_check
    assert found(store_id, "lychees") == []
    assert last_sync(api, made["id"])["awaiting_check"] == 1

    # No sync retries an email item: the awaiting-check sweep does.
    jev_classifier.down = False
    verdicts.sweep(store_id)
    assert items(store_id, made["id"])["late@x"].verdict == Verdict.clean
    assert len(found(store_id, "lychees")) == 1
    assert run(store_id, made["id"]) is None


def test_the_inbound_endpoint_needs_its_token(api, mailer, store_id):
    made = add(api)
    assert mail(mailer, made["email_address"], token="wrong").status_code == 401
    assert mailer.post("/api/inbound-mail", json={}).status_code == 401
    assert mail(mailer, made["email_address"], attachments=[{"content": "!!"}]).status_code == 422
    assert items(store_id, made["id"]) == {}


def test_email_is_unavailable_without_an_inbound_mail_domain(api, mailer, monkeypatch):
    monkeypatch.setattr(get_settings(), "inbound_mail_domain", None)
    kinds = {k["kind"]: k["unavailable"] for k in api.get("/api/source-kinds").json()}
    assert "inbound mail service" in kinds["email"]
    assert kinds["webhook"] is None
    resp = api.post("/api/sources", json={"name": "Inbox", "kind": "email"})
    assert resp.status_code == 422
    assert "roadmap" in resp.json()["detail"]
    assert mail(mailer, "x@" + DOMAIN).status_code == 404
