"""The inflow loop's seams for the full-stack test (`frontend/fullstack/inflow.spec.ts`): the
test provider's feeds and pages shared between the stack's processes through Redis
(`sources_test_feeds_shared`), changed upstream through its testing routes (`aios.testing.
feeds_api`), and a URL source reading one of its pages at `https://test.invalid/pages/...`.
And every way a document comes in (uploading, a synced file item, a removed connector's content
kept) gives it a source."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import uuid

import pytest
from fastapi.testclient import TestClient
from test_connectors import connect, scoped
from test_documents import upload
from test_sync import found, items, offline, run, store_id  # noqa: F401
from test_url_sources import add

from aios import url_fetch
from aios.auth import current_user
from aios.config import get_settings
from aios.main import create_app
from aios.source_providers import SourceView
from aios.source_providers import test as provider

PAGE = "https://test.invalid/pages/{}/prices.md"


@pytest.fixture
def shared(monkeypatch):
    """The test provider's feeds and pages shared through Redis, as in the full-stack stack;
    a fresh name for this test's feed and page, forgotten afterwards."""
    monkeypatch.setattr(get_settings(), "sources_test_feeds_shared", True)
    name = f"inflow-{uuid.uuid4().hex[:8]}"
    yield name
    provider.forget(name)
    provider.remove_page(f"{name}/prices.md")


def another_process():
    """What another of the stack's processes knows: nothing of its own."""
    provider.FEEDS.clear()
    provider.PAGES.clear()


def view(name: str) -> SourceView:
    return SourceView(id=1, name=name, settings={"feed": name})


@pytest.fixture
def testing_api(user, files_bucket, monkeypatch, shared):
    """The app as the full-stack stack runs it (its testing routes mounted), signed in."""
    app = create_app()
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


# --- Shared feeds --------------------------------------------------------------------------------


def test_a_feed_changed_in_one_process_is_what_another_syncs(shared):
    provider.feed(shared).message("zebrafish", "Zebrafish embryos are transparent.")
    provider.save(shared)
    another_process()

    listed = provider.TestProvider().listing(view(shared))

    assert [i.external_id for i in listed.changed] == ["zebrafish"]
    content = provider.TestProvider().fetch(view(shared), listed.changed[0])
    assert content.text == "Zebrafish embryos are transparent."


def test_an_item_deleted_in_one_process_is_a_removal_another_syncs(shared):
    provider.feed(shared).message("zebrafish", "Zebrafish embryos are transparent.")
    provider.save(shared)
    cursor = provider.TestProvider().changes(view(shared), None).cursor

    provider.feed(shared).delete("zebrafish")
    provider.save(shared)
    another_process()

    changes = provider.TestProvider().changes(view(shared), cursor)
    assert (changes.changed, changes.removed) == ([], ["zebrafish"])


def test_a_message_posted_by_the_tool_reaches_the_shared_feed(shared):
    provider.feed(shared)
    provider.save(shared)
    another_process()

    said = provider._post({"channel": shared, "text": "Hello from the agent."})

    another_process()
    assert said.startswith("Posted ")
    assert [e.content.text for e in provider.load(shared).items.values()] == [
        "Hello from the agent."
    ]


def test_unshared_feeds_stay_in_the_process(monkeypatch):
    name = f"local-{uuid.uuid4().hex[:8]}"
    provider.feed(name).message("m1", "Only here.")
    provider.save(name)
    another_process()

    assert provider.load(name) is None


# --- Pages ---------------------------------------------------------------------------------------


def test_a_test_page_is_fetched_in_the_process_and_gone_once_removed(shared):
    provider.page(f"{shared}/prices.md", "Widgets cost four pounds.")
    another_process()

    fetched = url_fetch.fetch(PAGE.format(shared))
    assert (fetched.status, fetched.body, fetched.content_type) == (
        url_fetch.OK,
        b"Widgets cost four pounds.",
        "text/markdown",
    )

    provider.remove_page(f"{shared}/prices.md")
    assert url_fetch.fetch(PAGE.format(shared)).status == url_fetch.GONE


def test_test_pages_are_unreachable_without_the_test_provider(shared, monkeypatch):
    provider.page(f"{shared}/prices.md", "Widgets cost four pounds.")
    monkeypatch.setattr(get_settings(), "sources_test_provider_enabled", False)

    with pytest.raises(url_fetch.Unreachable):
        url_fetch.check(PAGE.format(shared))


def test_a_url_source_syncs_a_test_page_into_a_document_of_its_source(
    api, store_id, shared, run_service
):
    run_service.auto = False
    provider.page(f"{shared}/prices.md", "Widgets cost four pounds.")
    resp = add(api, PAGE.format(shared))
    assert resp.status_code == 201, resp.text
    source = resp.json()

    assert run(store_id, source["id"]).added == 1

    [doc] = [d for d in api.get("/api/documents").json() if d["source"]["id"] == source["id"]]
    assert doc["filename"] == "test.invalid-pages-" + shared + "-prices.md"
    assert doc["source"]["name"] == "Price page"
    assert found(store_id, "widgets") == ["Widgets cost four pounds."]


# --- Its testing routes --------------------------------------------------------------------------


def test_the_testing_routes_change_a_feed_and_a_page_upstream(testing_api, shared):
    api = testing_api
    put = api.put(
        f"/api/testing/feeds/{shared}/messages/zebrafish",
        json={"text": "Zebrafish embryos are transparent.", "title": "Zebrafish"},
    )
    assert put.status_code == 204, put.text
    assert (
        api.put(f"/api/testing/pages/{shared}/prices.md", json={"text": "Four pounds."}).status_code
        == 204
    )
    another_process()

    feed = api.get(f"/api/testing/feeds/{shared}").json()
    assert feed == {
        "name": shared,
        "items": [
            {"id": "zebrafish", "title": "Zebrafish", "text": "Zebrafish embryos are transparent."}
        ],
    }
    assert url_fetch.fetch(PAGE.format(shared)).body == b"Four pounds."

    assert api.delete(f"/api/testing/feeds/{shared}/items/zebrafish").status_code == 204
    another_process()
    assert api.get(f"/api/testing/feeds/{shared}").json()["items"] == []
    assert provider.TestProvider().listing(view(shared)).changed == []


def test_the_testing_routes_need_a_signed_in_person(user, files_bucket, shared):
    anonymous = TestClient(create_app())
    assert anonymous.get(f"/api/testing/feeds/{shared}").status_code == 401


def test_the_testing_routes_dont_exist_unless_feeds_are_shared(api):
    assert api.get("/api/testing/feeds/anything").status_code == 404


def test_the_api_refuses_to_share_feeds_without_the_test_provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "sources_test_feeds_shared", True)
    monkeypatch.setattr(get_settings(), "sources_test_provider_enabled", False)
    with pytest.raises(RuntimeError, match="SOURCES_TEST_FEEDS_SHARED"):
        create_app()


# --- Every document has a source -----------------------------------------------------------------


def test_uploads_synced_files_and_kept_content_each_give_documents_with_a_source(
    api, store_id, shared, run_service
):
    # Uploading.
    uploaded = upload(api, "notes.md", b"Uploaded notes.", "text/markdown").json()
    # A synced file item, and a message item, on a connector's source.
    channel = provider.feed(shared)
    channel.file("f1", b"Cuttlefish bones float.", "cuttlebone.md")
    channel.message("m1", "Cuttlefish have three hearts.")
    connector_id = connect(api)
    source_id = scoped(api, connector_id, [channel.name])
    run(store_id, source_id)
    docs = {d["filename"]: d for d in api.get("/api/documents").json()}
    assert docs["notes.md"]["source"]["name"] == "Uploads"
    assert docs["notes.md"]["id"] == uploaded["id"]
    assert docs["cuttlebone.md"]["source"]["id"] == source_id

    # Removing the connector, keeping its content as documents.
    assert api.delete(f"/api/connectors/{connector_id}", params={"keep": True}).status_code == 202

    docs = {d["filename"]: d for d in api.get("/api/documents").json()}
    kept = [n for n in docs if n.startswith(f"{channel.name} ")]
    assert len(kept) == 1
    assert {docs[n]["source"]["name"] for n in [*kept, "cuttlebone.md", "notes.md"]} == {"Uploads"}
    assert items(store_id, source_id) == {}
