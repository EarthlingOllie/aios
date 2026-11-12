"""URL sources (`aios.url_sources`): a web page added by an admin, fetched by its syncs (the sync
worker entry point, `aios.sync.run_sync`) through the request-forgery guard, over a fake network
(`test_url_fetch.Net`). Outcomes: what's searchable, its document, its events."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import httpx
import pytest
from sqlalchemy import select
from test_documents import upload
from test_event_dispatcher import dispatch, fired
from test_event_triggers import document_trigger
from test_sync import INJECTION, found, items, offline, run, source_of, store_id  # noqa: F401
from test_triggers import make_agent, make_trigger
from test_url_fetch import PUBLIC, Net, page

from aios import url_fetch
from aios.datastores import store_session
from aios.datastores.models import Document, Event, SyncKind
from aios.jev import Verdict


@pytest.fixture
def net(monkeypatch) -> Net:
    net = Net()
    net.names["example.com"] = [PUBLIC]
    monkeypatch.setattr(url_fetch, "resolve", net.resolve)
    monkeypatch.setattr(url_fetch, "transport", lambda: httpx.MockTransport(net.handle))
    return net


def add(api, url: str, name: str = "Price page"):
    return api.post(
        "/api/sources", json={"name": name, "kind": "url", "url": url, "interval_seconds": 900}
    )


@pytest.fixture
def source(api, net, run_service) -> int:
    """A URL source for example.com/prices, its first sync not run yet."""
    run_service.auto = False
    resp = add(api, "https://example.com/prices")
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def documents(store_id, source_id) -> list[Document]:
    with store_session(store_id, "ro") as store:
        rows = store.scalars(select(Document).where(Document.source_id == source_id)).all()
        for r in rows:
            store.expunge(r)
        return list(rows)


def test_an_admin_adds_a_url_source(api, net, run_service):
    run_service.auto = False
    resp = add(api, "https://example.com/prices")
    assert resp.status_code == 201, resp.text
    made = resp.json()
    assert (made["kind"], made["url"], made["interval_seconds"]) == (
        "url",
        "https://example.com/prices",
        900,
    )
    kinds = {k["kind"]: k["unavailable"] for k in api.get("/api/source-kinds").json()}
    assert kinds["url"] is None


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://169.254.169.254/latest/", "https://example.com:8443/", "ftp://x"],
)
def test_saving_an_address_aios_cant_reach_is_refused_with_why(api, net, url):
    resp = add(api, url)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "This address isn't reachable from aios"


def test_changing_the_url_to_one_aios_cant_reach_is_refused(api, source):
    resp = api.patch(f"/api/sources/{source}", json={"url": "http://10.0.0.1/"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "This address isn't reachable from aios"
    assert source_of(api, source)["url"] == "https://example.com/prices"


def test_the_page_is_ingested_once_and_not_again_while_its_unchanged(api, store_id, net, source):
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.", etag='"v1"')

    first = run(store_id, source)
    assert (first.added, first.status) == (1, "succeeded")
    [doc] = documents(store_id, source)
    assert doc.filename == "example.com-prices.html"
    assert found(store_id, "widgets") == ["Widgets cost four pounds."]

    # Not modified: not fetched again, not ingested again.
    def conditional(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return page(b"Widgets cost four pounds.", etag='"v1"')

    net.pages[(PUBLIC, "/prices")] = conditional
    again = run(store_id, source)
    assert (again.added, again.updated) == (0, 0)
    assert net.requests[-1].headers["if-none-match"] == '"v1"'
    # Fetched whole (a full resync), the same content isn't ingested again either.
    full = run(store_id, source, SyncKind.full)
    assert (full.added, full.updated, full.removed) == (0, 0, 0)
    [same] = documents(store_id, source)
    assert (same.id, same.sha256) == (doc.id, doc.sha256)


def test_a_changed_page_replaces_its_documents_content(api, store_id, net, source):
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.")
    run(store_id, source)
    [doc] = documents(store_id, source)

    net.pages[(PUBLIC, "/prices")] = page(b"Gadgets cost nine pounds.")
    changed = run(store_id, source)

    assert changed.updated == 1
    [replaced] = documents(store_id, source)
    assert replaced.id == doc.id
    assert found(store_id, "gadgets") == ["Gadgets cost nine pounds."]
    assert found(store_id, "widgets") == []


@pytest.mark.parametrize("gone", [404, 410])
def test_not_found_or_gone_removes_the_document_and_the_source_stays_active(
    api, store_id, net, source, gone
):
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.")
    run(store_id, source)

    net.pages[(PUBLIC, "/prices")] = httpx.Response(gone)
    removed = run(store_id, source)

    assert (removed.status, removed.removed) == ("succeeded", 1)
    assert documents(store_id, source) == []
    assert found(store_id, "widgets") == []
    shown = source_of(api, source)
    assert (shown["status"], shown["health"]) == ("active", "healthy")
    # It keeps trying: back again, it's ingested again.
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets are back.")
    assert run(store_id, source).added == 1
    assert found(store_id, "widgets") == ["Widgets are back."]


def test_a_page_whose_address_turns_private_fails_the_sync_with_why(api, store_id, net, source):
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.")
    run(store_id, source)
    net.names["example.com"] = ["10.0.0.9"]

    failed = run(store_id, source)

    assert failed.status == "failed"
    assert failed.error == "This address isn't reachable from aios"
    assert net.connected.count("10.0.0.9") == 0
    # What it had stays.
    assert len(documents(store_id, source)) == 1


def test_a_redirect_to_a_private_address_fails_the_sync(api, store_id, net, source):
    net.names["inside.example"] = ["192.168.0.10"]
    net.pages[(PUBLIC, "/prices")] = httpx.Response(
        302, headers={"location": "http://inside.example/"}
    )
    failed = run(store_id, source)
    assert failed.status == "failed"
    assert failed.error == "This address isn't reachable from aios"
    assert documents(store_id, source) == []


def test_a_flagged_page_isnt_searchable_and_fires_nothing(api, store_id, net, source):
    net.pages[(PUBLIC, "/prices")] = page(f"Widget prices. {INJECTION}".encode())
    run(store_id, source)
    [item] = items(store_id, source).values()
    assert item.verdict == Verdict.flagged
    assert found(store_id, "widget") == []
    assert ingested(store_id) == []


def test_a_page_awaiting_check_becomes_searchable_once_a_retry_finds_it_clean(
    api, store_id, net, source, jev_classifier
):
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.", etag='"v1"')
    jev_classifier.down = True
    waited = run(store_id, source)
    assert waited.awaiting_check == 1
    assert found(store_id, "widgets") == []
    assert ingested(store_id) == []

    jev_classifier.down = False
    net.pages[(PUBLIC, "/prices")] = lambda request: httpx.Response(304)
    checked = run(store_id, source)

    assert checked.awaiting_check == 0
    assert found(store_id, "widgets") == ["Widgets cost four pounds."]
    [event] = ingested(store_id)
    assert event["source_id"] == source


def ingested(store_id) -> list[dict]:
    with store_session(store_id, "ro") as store:
        return [e.payload for e in store.scalars(select(Event)) if e.type == "document_ingested"]


def test_a_trigger_filtered_to_a_source_fires_for_its_documents_not_for_uploads(
    api, store_id, net, source
):
    agent = make_agent(api, "Price watcher")
    watching = make_trigger(
        api, document_trigger("agent", agent["id"], filter={"sources": [source]})
    )
    uploads_id = next(s["id"] for s in api.get("/api/sources").json() if s["kind"] == "upload")
    uploads = make_trigger(
        api, document_trigger("agent", agent["id"], filter={"sources": [uploads_id]})
    )
    net.pages[(PUBLIC, "/prices")] = page(b"Widgets cost four pounds.")
    assert upload(api, "notes.md", b"Notes from the meeting.", "text/markdown").status_code == 201

    run(store_id, source)
    dispatch()

    events = ingested(store_id)
    assert sorted((e["name"], e["source_id"]) for e in events) == [
        ("example.com-prices.html", source),
        ("notes.md", uploads_id),
    ]
    [fired_for_page] = fired(watching["id"])
    [fired_for_upload] = fired(uploads["id"])
    assert fired_for_page.event_id != fired_for_upload.event_id
    with store_session(store_id, "ro") as store:
        page_event = store.get(Event, fired_for_page.event_id)
        assert page_event.payload["source_id"] == source
