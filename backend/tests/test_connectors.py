"""Connectors (`aios.connectors`): connecting an external system with a signed state, choosing
what it syncs, the calls it may make, and ending the connection, shown with the test provider
(`aios.source_providers.test`). Seen through the API, as an admin and the other roles, and through
the sync worker entry point (`aios.sync.run_sync`), as the run service calls it."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import join_organisation, store_of
from fastapi.testclient import TestClient
from sqlalchemy import func, text, update
from test_chats import signed_in_as
from test_invitations import leave_behind
from test_permissions import participant, set_role  # noqa: F401  (a fixture)
from test_search import embedded  # noqa: F401  (a fixture)
from test_sync import found, items, offline, run  # noqa: F401  (a fixture)

from aios import connectors, sync
from aios.config import get_settings
from aios.datastores import files, store_session
from aios.datastores.models import Connector, Document
from aios.db import SessionLocal
from aios.main import app
from aios.models import ConnectorCredential, Role
from aios.run_service import sweep
from aios.source_providers import test as provider

WARNING = (
    "Some of what you picked is restricted at its source. Once it's synced, everyone in the "
    "organisation will be able to search it."
)


@pytest.fixture
def store_id(user, files_bucket) -> uuid.UUID:
    return store_of(user)


@pytest.fixture
def editor(db, user):
    u = join_organisation(db, user, "editor", first_name="Ed", last_name="Editor")
    set_role(db, u, Role.editor, of=user)
    yield u
    leave_behind(db, u)


@pytest.fixture
def feeds():
    """Make test feeds (the test provider's channels), forgotten afterwards."""
    made: list[str] = []

    def make(restricted: bool = False) -> provider.Feed:
        name = f"chan-{uuid.uuid4().hex[:8]}"
        made.append(name)
        f = provider.feed(name)
        f.restricted = restricted
        return f

    yield make
    for name in made:
        provider.forget(name)


def start(api, provider_name: str = "test"):
    return api.post("/api/connectors", json={"provider": provider_name})


def callback(client, redirect_url: str, **override):
    """Follow a connect flow's redirect to the callback, as the provider would send the browser
    back, with any of its query (`state`, `code`) replaced."""
    parts = urlsplit(redirect_url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()} | override
    return client.get(parts.path, params=query, follow_redirects=False)


def connect(api) -> int:
    resp = start(api)
    assert resp.status_code == 200, resp.text
    back = callback(api, resp.json()["redirect_url"])
    assert back.status_code == 303, back.text
    return int(back.headers["location"].rsplit("/", 1)[1])


def connector_of(api, connector_id: int) -> dict:
    resp = api.get(f"/api/connectors/{connector_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def set_scope(api, connector_id: int, ids: list[str], acknowledge: bool = False):
    return api.put(
        f"/api/connectors/{connector_id}/scope",
        json={"items": ids, "acknowledge_restricted": acknowledge},
    )


def scoped(api, connector_id: int, ids: list[str]) -> int:
    """Set the connector's scope (acknowledging anything restricted); its source's id."""
    resp = set_scope(api, connector_id, ids, acknowledge=True)
    assert resp.status_code == 200, resp.text
    return resp.json()["source_id"]


def stored_text(store_id, document_id: int) -> str:
    """A document's file, as stored."""
    with store_session(store_id, "ro") as store:
        key = store.get_one(Document, document_id).s3_key
    return files.read(key).decode()


def audit_actions(api) -> list[tuple[str, dict]]:
    resp = api.get("/api/audit-log", params={"action": "connector"})
    assert resp.status_code == 200, resp.text
    return [(e["action"], e["details"]) for e in reversed(resp.json()["entries"])]


# --- Providers -----------------------------------------------------------------------------------


def test_the_providers_list_says_what_this_deployment_offers_and_why_not(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "slack_client_id", "")

    listed = {p["provider"]: p for p in api.get("/api/connector-providers").json()}

    assert set(listed) == {"slack", "google_drive", "github", "test"}
    assert listed["test"]["unavailable"] is None
    assert "app settings" in listed["slack"]["unavailable"]
    assert listed["slack"]["title"] == "Slack"

    monkeypatch.setattr(get_settings(), "connector_enabled_providers", ["slack"])
    listed = {p["provider"]: p for p in api.get("/api/connector-providers").json()}
    assert "switched off" in listed["test"]["unavailable"]


# --- Connecting ----------------------------------------------------------------------------------


def test_an_admin_connects_through_a_signed_state_and_lands_on_the_connector(api):
    resp = start(api)
    assert resp.status_code == 200, resp.text

    back = callback(api, resp.json()["redirect_url"])

    assert back.status_code == 303, back.text
    connector_id = int(back.headers["location"].rsplit("/", 1)[1])
    assert "/data/connectors/" in back.headers["location"]
    connector = connector_of(api, connector_id)
    assert {k: connector[k] for k in ("provider", "status", "unavailable", "scope")} == {
        "provider": "test",
        "status": "active",
        "unavailable": None,
        "scope": [],
    }
    assert [c["id"] for c in api.get("/api/connectors").json()] == [connector_id]


def test_only_admins_start_a_connection(api, editor, participant):
    for who in (editor, participant):
        with signed_in_as(who):
            assert start(api).status_code == 403
    assert api.get("/api/connectors").json() == []


def test_one_connector_per_provider(api):
    # Two flows started before either finishes: the second to come back is refused.
    first, second = start(api).json(), start(api).json()
    assert callback(api, first["redirect_url"]).status_code == 303
    assert callback(api, second["redirect_url"]).status_code == 409

    assert start(api).status_code == 409
    assert len(api.get("/api/connectors").json()) == 1


def test_an_unavailable_provider_cant_be_connected(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "slack_client_id", "")
    assert start(api, "slack").status_code == 409
    assert start(api, "nope").status_code == 409


def test_a_bad_or_expired_state_is_refused_with_400(api, monkeypatch):
    redirect = start(api).json()["redirect_url"]

    assert callback(api, redirect, state="not-a-state").status_code == 400
    state = parse_qs(urlsplit(redirect).query)["state"][0]
    assert callback(api, redirect, state=state[:-2] + "xx").status_code == 400

    monkeypatch.setattr(get_settings(), "connector_state_ttl_seconds", -1)
    assert callback(api, redirect).status_code == 400
    assert api.get("/api/connectors").json() == []


def test_a_state_from_another_session_or_person_is_refused_with_403(api, new_user):
    redirect = start(api).json()["redirect_url"]

    # Another browser: the same person, but not the session that started it.
    other_browser = TestClient(app)
    assert callback(other_browser, redirect).status_code == 403
    # This browser, someone else signed in.
    with signed_in_as(new_user()):
        assert callback(api, redirect).status_code == 403
    assert api.get("/api/connectors").json() == []


def test_connecting_is_recorded_in_the_audit_log(api):
    connector_id = connect(api)
    assert audit_actions(api) == [("connector.connected", {"name": "Test", "provider": "test"})]
    assert connector_of(api, connector_id)["name"] == "Test"


# --- Choosing what syncs -------------------------------------------------------------------------


def test_scope_options_are_searchable_and_mark_whats_restricted(api, feeds):
    public, private = feeds(), feeds(restricted=True)
    connector_id = connect(api)

    resp = api.get(f"/api/connectors/{connector_id}/scope-options", params={"q": private.name})

    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"id": private.name, "name": private.name, "restricted": True}]
    every = api.get(f"/api/connectors/{connector_id}/scope-options").json()
    assert {"id": public.name, "name": public.name, "restricted": False} in every


def test_picking_something_restricted_needs_the_acknowledgement(api, feeds):
    public, private = feeds(), feeds(restricted=True)
    connector_id = connect(api)

    refused = set_scope(api, connector_id, [public.name, private.name])

    assert refused.status_code == 422
    assert refused.json()["detail"] == WARNING
    assert connector_of(api, connector_id)["scope"] == []
    accepted = set_scope(api, connector_id, [public.name, private.name], acknowledge=True)
    assert accepted.status_code == 200, accepted.text
    assert [(i["id"], i["restricted"]) for i in accepted.json()["scope"]] == [
        (public.name, False),
        (private.name, True),
    ]


def test_something_the_connector_cant_sync_is_refused(api, feeds):
    connector_id = connect(api)
    assert set_scope(api, connector_id, ["no-such-channel"]).status_code == 422


def test_only_admins_choose_what_syncs(api, feeds, participant, editor):
    channel = feeds()
    connector_id = connect(api)
    for who in (editor, participant):
        with signed_in_as(who):
            assert set_scope(api, connector_id, [channel.name]).status_code == 403
            assert api.get(f"/api/connectors/{connector_id}/scope-options").status_code == 403
            # Seeing the connector is everyone's.
            assert connector_of(api, connector_id)["id"] == connector_id


def test_saving_the_scope_makes_a_source_that_syncs_for_every_member_to_search(
    api, store_id, feeds, participant, embedded
):
    channel, elsewhere = feeds(), feeds()
    channel.message("m1", "The narwhal's tusk is a tooth.")
    elsewhere.message("m1", "The narwhal migrates with the ice.")
    connector_id = connect(api)

    source_id = scoped(api, connector_id, [channel.name])

    # Its first sync was queued, and the run service ran it.
    assert found(store_id, "narwhal") == ["The narwhal's tusk is a tooth."]
    connector = connector_of(api, connector_id)
    assert connector["source"]["id"] == source_id
    assert connector["source"]["health"] == "healthy"
    assert connector["source"]["item_count"] == 1
    with signed_in_as(participant):
        hits = api.get("/api/search", params={"q": "narwhal"}).json()
    assert [h["text"] for h in hits] == ["The narwhal's tusk is a tooth."]


def test_narrowing_the_scope_removes_the_dropped_items_at_the_next_sync(api, store_id, feeds):
    kept, dropped = feeds(), feeds()
    kept.message("m1", "Puffins nest in burrows.")
    dropped.message("m1", "Puffins dive for sand eels.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [kept.name, dropped.name])
    assert len(found(store_id, "puffins")) == 2

    resp = set_scope(api, connector_id, [kept.name])
    assert resp.status_code == 200, resp.text

    assert found(store_id, "puffins") == ["Puffins nest in burrows."]
    assert set(items(store_id, source_id)) == {f"{kept.name}/m1"}


def test_scope_changes_are_recorded_in_the_audit_log(api, feeds):
    a, b = feeds(), feeds(restricted=True)
    connector_id = connect(api)
    scoped(api, connector_id, [a.name, b.name])
    set_scope(api, connector_id, [b.name], acknowledge=True)

    assert audit_actions(api)[1:] == [
        (
            "connector.scope_changed",
            {
                "name": "Test",
                "added": [a.name, b.name],
                "removed": [],
                "restricted_acknowledged": True,
            },
        ),
        (
            "connector.scope_changed",
            {"name": "Test", "added": [], "removed": [a.name], "restricted_acknowledged": True},
        ),
    ]


# --- Access failing, and reconnecting ------------------------------------------------------------


def test_a_final_authorisation_failure_needs_reconnecting_and_stops_syncing(api, store_id, feeds):
    channel = feeds()
    channel.message("m1", "Otters hold hands while sleeping.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])

    channel.revoked = True
    failed = run(store_id, source_id)

    assert failed.status == "failed"
    connector = connector_of(api, connector_id)
    assert connector["status"] == "needs_reauth"
    assert connector["source"]["health"] == "needs_reconnecting"
    # It's no longer due, and Sync now isn't run either: its syncs have stopped.
    with store_session(store_id, "ro") as store:
        assert source_id not in sync.due(store)
    # Picking what syncs waits for reconnecting.
    assert api.get(f"/api/connectors/{connector_id}/scope-options").status_code == 409


def test_reconnecting_resumes_from_the_stored_cursor(api, store_id, feeds):
    channel = feeds()
    channel.message("m1", "Seahorse fathers carry the young.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    assert channel.fetched == ["m1"]
    channel.revoked = True
    run(store_id, source_id)
    assert connector_of(api, connector_id)["status"] == "needs_reauth"

    # Access granted again at the provider, and the admin reconnects.
    channel.revoked = False
    channel.message("m2", "Seahorses swim upright.")
    resp = api.post(f"/api/connectors/{connector_id}/reconnect")
    assert resp.status_code == 200, resp.text
    back = callback(api, resp.json()["redirect_url"])
    assert back.status_code == 303, back.text

    connector = connector_of(api, connector_id)
    assert connector["status"] == "active"
    assert connector["source"]["health"] == "healthy"
    # The sync queued on reconnecting fetched only what's new since the cursor.
    assert channel.fetched == ["m1", "m2"]
    assert sorted(found(store_id, "seahorse")) == [
        "Seahorse fathers carry the young.",
        "Seahorses swim upright.",
    ]
    assert [a for a, _ in audit_actions(api)][-1] == "connector.reconnected"


# --- Calls only to the provider's hosts ----------------------------------------------------------


def test_a_call_to_a_host_outside_the_allowed_list_is_refused_and_recorded_on_the_sync(
    api, store_id, feeds
):
    channel = feeds()
    channel.message("m1", "Krill swarm in the Southern Ocean.")
    channel.calls = ["https://test.invalid/api/history"]
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    assert found(store_id, "krill") == ["Krill swarm in the Southern Ocean."]
    # The provider was called with the connector's token.
    assert len(channel.tokens) == 1 and channel.tokens[0].startswith("test-token-")

    channel.calls = ["https://evil.example/steal"]
    refused = run(store_id, source_id)

    assert refused.status == "failed"
    assert "evil.example" in refused.error and "allowed hosts" in refused.error
    [last] = connector_of(api, connector_id)["source"]["last_sync"]["error"].splitlines()[:1]
    assert "evil.example" in last


def test_switching_a_provider_off_makes_its_connectors_unavailable_at_once_and_keeps_their_content(
    api, store_id, feeds, monkeypatch
):
    channel = feeds()
    channel.message("m1", "Manta rays have no stinger.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])

    monkeypatch.setattr(get_settings(), "connector_enabled_providers", ["slack"])

    connector = connector_of(api, connector_id)
    assert "switched off" in connector["unavailable"]
    channel.message("m2", "Manta rays visit cleaning stations.")
    assert run(store_id, source_id) is None
    assert channel.fetched == ["m1"]
    assert found(store_id, "manta") == ["Manta rays have no stinger."]


# --- Removing ------------------------------------------------------------------------------------


def test_removing_with_delete_leaves_no_items_documents_or_search_entries(api, store_id, feeds):
    channel = feeds()
    channel.message("m1", "Lanternfish glow in the deep.")
    channel.file("f1", b"Lanternfish migrate vertically each night.", "lanternfish.md")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    assert len(found(store_id, "lanternfish")) == 2

    resp = api.delete(f"/api/connectors/{connector_id}", params={"keep": False})

    assert resp.status_code == 202, resp.text
    assert api.get("/api/connectors").json() == []
    assert found(store_id, "lanternfish") == []
    assert api.get("/api/documents").json() == []
    assert source_id not in [s["id"] for s in api.get("/api/sources").json()]
    assert items(store_id, source_id) == {}
    assert ("connector.removed", {"name": "Test", "keep_documents": False}) in audit_actions(api)


def test_removing_with_keep_makes_uploads_documents_that_survive(api, store_id, feeds):
    channel = feeds()
    october, november = datetime(2026, 10, 3, 9, tzinfo=UTC), datetime(2026, 11, 2, 9, tzinfo=UTC)
    channel.message("t1", "Cuttlefish change colour.", author_name="Ana", updated_at=october)
    channel.message(
        "r1",
        "They're colour-blind!",
        author_name="Ben",
        updated_at=october,
        parent_external_id="t1",
    )
    channel.message("t2", "Cuttlefish have three hearts.", author_name="Ana", updated_at=november)
    channel.file("f1", b"Cuttlefish bones float.", "cuttlebone.md")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])

    resp = api.delete(f"/api/connectors/{connector_id}", params={"keep": True})

    assert resp.status_code == 202, resp.text
    docs = {d["filename"]: d for d in api.get("/api/documents").json()}
    assert set(docs) == {
        "cuttlebone.md",
        f"{channel.name} 2026-10.md",
        f"{channel.name} 2026-11.md",
    }
    assert {d["source"]["name"] for d in docs.values()} == {"Uploads"}
    text = stored_text(store_id, docs[f"{channel.name} 2026-10.md"]["id"])
    assert text.startswith(f"# {channel.name}, 2026-10")
    assert "**Ana**" in text and "Cuttlefish change colour." in text
    assert "> They're colour-blind!" in text
    # Searchable as uploads, and nothing of the source is left.
    assert any("three hearts" in t for t in found(store_id, "cuttlefish"))
    assert any("bones float" in t for t in found(store_id, "cuttlefish"))
    assert items(store_id, source_id) == {}
    # They're ordinary uploads now: deleting one is allowed.
    assert api.delete(f"/api/documents/{docs['cuttlebone.md']['id']}").status_code == 204


def test_removing_stops_the_connector_at_once_and_revokes_its_access(api, store_id, feeds):
    channel = feeds()
    channel.calls = ["https://test.invalid/api/history"]
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    [token] = channel.tokens

    api.delete(f"/api/connectors/{connector_id}", params={"keep": False})

    assert token in provider.REVOKED
    assert run(store_id, source_id) is None
    assert api.post(f"/api/connectors/{connector_id}/reconnect").status_code == 404


def test_only_admins_remove_a_connector(api, editor, participant):
    connector_id = connect(api)
    for who in (editor, participant):
        with signed_in_as(who):
            resp = api.delete(f"/api/connectors/{connector_id}", params={"keep": True})
            assert resp.status_code == 403
    assert connector_of(api, connector_id)["status"] == "active"


# --- Credentials ---------------------------------------------------------------------------------


def test_credentials_are_never_in_the_api_or_the_store(api, store_id, feeds, user):
    channel = feeds()
    channel.message("m1", "Sea stars regrow arms.")
    channel.calls = ["https://test.invalid/api/history"]
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    [token] = channel.tokens

    responses = [
        api.get("/api/connectors"),
        api.get(f"/api/connectors/{connector_id}"),
        api.get("/api/connector-providers"),
        api.get(f"/api/sources/{source_id}"),
        api.get("/api/sources"),
        api.get("/api/audit-log"),
    ]
    assert all(r.status_code == 200 for r in responses)
    assert not any(token in r.text for r in responses)
    with store_session(store_id, "rw") as store:
        tables = store.execute(
            text(
                "SELECT table_schema, table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'aios'"
                " AND data_type IN ('text', 'jsonb', 'character varying')"
            )
        ).all()
        for schema, table, column in tables:
            hit = store.execute(
                text(f'SELECT 1 FROM "{schema}"."{table}" WHERE "{column}"::text LIKE :t LIMIT 1'),
                {"t": f"%{token}%"},
            ).first()
            assert hit is None, f"{table}.{column}"
    # In the platform database, only encrypted.
    with SessionLocal() as db:
        [row] = db.query(ConnectorCredential).filter_by(connector_id=connector_id).all()
        assert token not in row.secret


def test_a_removal_that_stopped_part_way_is_finished_by_the_sweep(
    api, store_id, feeds, monkeypatch
):
    channel = feeds()
    channel.message("m1", "Blobfish look different at depth.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    # The process stopped before finishing: its removal was started, and the rest never ran.
    monkeypatch.setattr(connectors, "finish_removal", lambda *a: None)
    api.delete(f"/api/connectors/{connector_id}", params={"keep": False})
    monkeypatch.undo()
    assert found(store_id, "blobfish") == ["Blobfish look different at depth."]
    with store_session(store_id, "rw") as store:
        store.execute(update(Connector).values(updated_at=func.now() - timedelta(days=1)))
        store.commit()

    sweep._enqueue_syncs()

    assert found(store_id, "blobfish") == []
    assert items(store_id, source_id) == {}
    assert api.get("/api/connectors").json() == []


def test_a_connectors_source_is_removed_with_its_connector_only(api, feeds):
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [feeds().name])

    resp = api.delete(f"/api/sources/{source_id}")

    assert resp.status_code == 409
    assert "connector" in resp.json()["detail"]
