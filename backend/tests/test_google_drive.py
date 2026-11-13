"""The Google Drive connector (`aios.source_providers.google_drive`): an admin connects Drive, picks
folders (shared drives included), and everything under them syncs as documents, native Google
files exported to formats aios reads; Drive's change feed keeps the store current. Against a fake
Drive replaying recorded responses (`fake_google_drive`), through the API and the sync worker
entry point (`aios.sync.run_sync`)."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import store_of
from fake_google_drive import CLIENT_ID, CLIENT_SECRET, CODE, REFRESH_TOKEN, FakeDrive
from sqlalchemy import select
from test_connectors import connector_of, stored_text
from test_sync import found, items, offline, run  # noqa: F401  (a fixture)

from aios import connectors
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Document, SyncKind
from aios.db import SessionLocal
from aios.models import ConnectorCredential, CredentialKind
from aios.source_providers import google_drive as provider

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

POLICIES = "1PoliciesFolder"
HR = "1HrFolder"
ARCHIVE = "1ArchiveFolder"
PRIVATE = "1PrivateFolder"
ENGINEERING = "0AengDrive00PVA"


@pytest.fixture
def store_id(user, files_bucket) -> uuid.UUID:
    return store_of(user)


@pytest.fixture
def drive(monkeypatch) -> FakeDrive:
    """Google Drive, faked: the OAuth client's settings set, and its calls answered by the
    fake."""
    fake = FakeDrive()
    s = get_settings()
    monkeypatch.setattr(s, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(s, "google_client_secret", type(s.google_client_secret)(CLIENT_SECRET))
    monkeypatch.setattr(provider, "TRANSPORT", fake.transport())
    provider.forget_tokens()
    yield fake
    provider.forget_tokens()


def connect(api) -> int:
    """Connect Drive as an admin: aios sends the browser to Google's consent page, and Google
    sends it back with a code."""
    resp = api.post("/api/connectors", json={"provider": "google_drive"})
    assert resp.status_code == 200, resp.text
    state = parse_qs(urlsplit(resp.json()["redirect_url"]).query)["state"][0]
    back = api.get(
        "/api/connectors/callback",
        params={"code": CODE, "state": state},
        follow_redirects=False,
    )
    assert back.status_code == 303, back.text
    return int(back.headers["location"].rsplit("/", 1)[1])


def scoped(api, connector_id: int, folders: list[str]) -> int:
    resp = api.put(
        f"/api/connectors/{connector_id}/scope",
        json={"items": folders, "acknowledge_restricted": True},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["source_id"]


@pytest.fixture
def synced(api, drive, store_id, run_service) -> int:
    """Drive connected, with Policies and the Engineering shared drive picked; not synced yet."""
    run_service.auto = False
    return scoped(api, connect(api), [POLICIES, ENGINEERING])


def documents(api, source_id) -> dict[str, dict]:
    resp = api.get("/api/documents", params={"source_id": source_id})
    assert resp.status_code == 200, resp.text
    return {d["filename"]: d for d in resp.json()}


def content_types(store_id, source_id) -> dict[str, str]:
    """The source's documents' file names and content types, as stored."""
    with store_session(store_id, "ro") as store:
        return dict(
            store.execute(
                select(Document.filename, Document.content_type).where(
                    Document.source_id == source_id
                )
            ).all()
        )


# --- Availability --------------------------------------------------------------------------------


def providers(api) -> dict[str, dict]:
    return {p["provider"]: p for p in api.get("/api/connector-providers").json()}


def test_drive_is_unavailable_without_its_app_settings(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", "")

    assert "app settings" in providers(api)["google_drive"]["unavailable"]


def test_with_its_app_settings_drive_can_be_connected(api, drive):
    listed = providers(api)["google_drive"]

    assert (listed["title"], listed["unavailable"]) == ("Google Drive", None)


def test_an_unverified_production_deployment_lists_drive_unavailable_and_wont_connect_it(
    api, drive, monkeypatch
):
    monkeypatch.setattr(get_settings(), "cookie_secure", True)

    why = providers(api)["google_drive"]["unavailable"]
    resp = api.post("/api/connectors", json={"provider": "google_drive"})

    assert "until Google has verified" in why
    assert resp.status_code == 409, resp.text
    assert "until Google has verified" in resp.json()["detail"]
    assert drive.requests == []

    monkeypatch.setattr(get_settings(), "google_drive_verified", True)
    assert providers(api)["google_drive"]["unavailable"] is None


# --- Connecting ----------------------------------------------------------------------------------


def test_connecting_asks_google_for_read_only_offline_access_and_keeps_only_the_refresh_token(
    api, drive, store_id
):
    resp = api.post("/api/connectors", json={"provider": "google_drive"})
    url = urlsplit(resp.json()["redirect_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    assert (url.netloc, url.path) == ("accounts.google.com", "/o/oauth2/v2/auth")
    assert query["scope"] == "https://www.googleapis.com/auth/drive.readonly"
    assert (query["access_type"], query["response_type"]) == ("offline", "code")
    assert query["client_id"] == CLIENT_ID

    connector_id = connect(api)

    with SessionLocal() as db:
        row = db.scalar(
            select(ConnectorCredential).where(ConnectorCredential.connector_id == connector_id)
        )
        assert row.kind == CredentialKind.oauth
        assert row.external_account == "ada@acme.example"
    assert connectors.credential(store_id, connector_id) == REFRESH_TOKEN
    shown = str(connector_of(api, connector_id))
    assert REFRESH_TOKEN not in shown and "ya29" not in shown


def test_folders_and_shared_drives_can_be_picked_and_unshared_folders_are_restricted(api, drive):
    connector_id = connect(api)

    resp = api.get(f"/api/connectors/{connector_id}/scope-options")

    assert resp.status_code == 200, resp.text
    options = {o["id"]: (o["name"], o["restricted"]) for o in resp.json()}
    assert options == {
        ENGINEERING: ("Engineering (shared drive)", False),
        "1RunbooksFolder": ("Engineering / Runbooks", False),
        POLICIES: ("Policies", False),
        HR: ("Policies / HR", False),
        PRIVATE: ("Private notes", True),
        ARCHIVE: ("Archive", False),
    }


# --- Syncing -------------------------------------------------------------------------------------


def test_everything_under_the_chosen_folders_syncs_with_native_files_exported(
    api, drive, store_id, synced
):
    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert content_types(store_id, synced) == {
        "Leave policy.docx": DOCX,
        "Budget 2026.xlsx": XLSX,
        "All hands.pptx": PPTX,
        "Expenses.pdf": "application/pdf",
        "Deploying.docx": DOCX,
        "oncall.md": "text/markdown",
    }
    assert sorted(drive.exports) == [
        ("1AllHandsSlides", PPTX),
        ("1BudgetSheet", XLSX),
        ("1DeployDoc", DOCX),
        ("1LeaveDoc", DOCX),
    ]
    assert sorted(drive.downloads) == ["1ExpensesPdf", "1OncallMd"]
    # Nested folders (HR, under Policies) and the shared drive's folders come in too.
    assert found(store_id, "annual leave")
    assert found(store_id, "cormorant cluster")
    assert found(store_id, "albatross standup")
    # Outside the chosen folders: not synced.
    assert not found(store_id, "Orkney")
    assert not found(store_id, "narwhal")
    leave = items(store_id, synced)["1LeaveDoc"]
    assert leave.url == "https://drive.google.com/open?id=1LeaveDoc"
    assert leave.author_name == "Hana HR"


def test_unsupported_and_oversized_files_are_listed_as_skipped_on_the_sync_record(
    api, drive, store_id, synced
):
    record = run(store_id, synced)

    assert record.dropped == 3
    resp = api.get(f"/api/sources/{synced}")
    assert resp.status_code == 200, resp.text
    [last] = resp.json()["syncs"]
    assert sorted((s["name"], s["reason"]) for s in last["skipped_files"]) == [
        ("Handbook scans.pdf", "Larger than the 50 MiB file cap."),
        ("Offsite feedback", "Google Forms files can't be exported."),
        ("logo.png", "Unsupported file type (image/png)."),
    ]
    assert "1ScansPdf" not in drive.downloads


def test_an_edited_doc_is_re_exported_and_re_ingested_and_unchanged_files_arent_fetched(
    api, drive, store_id, synced
):
    run(store_id, synced)
    exported = len(drive.exports)
    drive.edit("1LeaveDoc", "Leave policy. Staff now get thirty-five days of sabbatical leave.")

    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert record.kind == SyncKind.incremental
    assert (record.added, record.updated, record.removed) == (0, 1, 0)
    assert drive.exports[exported:] == [("1LeaveDoc", DOCX)]
    assert found(store_id, "sabbatical")
    assert not found(store_id, "annual leave")
    doc = documents(api, synced)["Leave policy.docx"]
    assert "sabbatical" in stored_text(store_id, doc["id"])
    # The change feed was read from the page token the last sync kept.
    assert any("/drive/v3/changes?" in url for _, url in drive.requests)


def test_files_moved_in_are_added_and_files_moved_out_trashed_deleted_or_unshared_are_removed(
    api, drive, store_id, synced
):
    drive.page_size = 2  # listings and the change feed read page by page
    run(store_id, synced)

    drive.move("1OldPlanDoc", HR)  # in
    drive.move("1BudgetSheet", ARCHIVE)  # out
    drive.trash("1AllHandsSlides")
    drive.delete("1ExpensesPdf")
    drive.unshare("1OncallMd")
    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert (record.added, record.removed) == (1, 4)
    assert set(documents(api, synced)) == {"Leave policy.docx", "Old plan.docx", "Deploying.docx"}
    assert found(store_id, "Orkney")
    assert not found(store_id, "offsite")
    assert not found(store_id, "lighthouse")
    assert not found(store_id, "Receipts")
    assert not found(store_id, "albatross")


def test_a_folder_moved_out_takes_its_files_with_it_and_one_moved_in_brings_its_files(
    api, drive, store_id, synced
):
    run(store_id, synced)

    drive.move(HR, ARCHIVE)
    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert "Leave policy.docx" not in documents(api, synced)
    assert not found(store_id, "annual leave")

    drive.move(ARCHIVE, POLICIES)
    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert {"Leave policy.docx", "Old plan.docx"} <= set(documents(api, synced))


def test_a_trashed_folder_removes_everything_under_it(api, drive, store_id, synced):
    run(store_id, synced)

    drive.trash("1RunbooksFolder")
    run(store_id, synced)

    assert "Deploying.docx" not in documents(api, synced)
    assert "oncall.md" in documents(api, synced)


def test_a_file_that_grows_past_the_cap_is_removed_and_listed_as_skipped(
    api, drive, store_id, synced, monkeypatch
):
    run(store_id, synced)
    monkeypatch.setattr(get_settings(), "drive_file_max_bytes", 70)
    drive.edit("1OncallMd", "# On call\n\n" + "The pager rotates every Tuesday. " * 4)

    record = run(store_id, synced)

    assert "oncall.md" not in documents(api, synced)
    assert record.removed == 1
    skipped = api.get(f"/api/sources/{synced}").json()["syncs"][0]["skipped_files"]
    assert [s["name"] for s in skipped] == ["oncall.md"]


def test_a_doc_too_large_for_google_to_export_is_listed_as_skipped(api, drive, store_id, synced):
    drive.too_large.add("1BudgetSheet")

    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert "Budget 2026.xlsx" not in documents(api, synced)
    skipped = api.get(f"/api/sources/{synced}").json()["syncs"][0]["skipped_files"]
    assert (
        "Budget 2026.xlsx",
        "Google couldn't export it: This file is too large to be exported.",
    ) in {(s["name"], s["reason"]) for s in skipped}


def test_reconciliation_removes_files_no_longer_under_the_chosen_folders(
    api, drive, store_id, synced
):
    run(store_id, synced)
    drive.delete("1ExpensesPdf", silently=True)

    run(store_id, synced)
    assert "Expenses.pdf" in documents(api, synced)  # the change feed never said

    record = run(store_id, synced, SyncKind.full)

    assert (record.status, record.removed) == ("succeeded", 1)
    assert "Expenses.pdf" not in documents(api, synced)


def test_narrowing_the_scope_removes_a_shared_drives_files(api, drive, store_id, synced):
    run(store_id, synced)
    connector_id = connector_of_source(api, synced)

    scoped(api, connector_id, [POLICIES])
    run(store_id, synced)

    assert set(documents(api, synced)) == {
        "Leave policy.docx",
        "Budget 2026.xlsx",
        "All hands.pptx",
        "Expenses.pdf",
    }
    assert not found(store_id, "cormorant")


def connector_of_source(api, source_id: int) -> int:
    [connector] = [c for c in api.get("/api/connectors").json() if c["source_id"] == source_id]
    return connector["id"]


def test_a_shared_drive_alone_can_be_chosen_and_synced(api, drive, store_id, run_service):
    run_service.auto = False
    source_id = scoped(api, connect(api), [ENGINEERING])

    record = run(store_id, source_id)

    assert record.status == "succeeded", record.error
    assert set(documents(api, source_id)) == {"Deploying.docx", "oncall.md"}

    drive.put(
        "1PostmortemDoc",
        "Postmortem",
        "application/vnd.google-apps.document",
        "1RunbooksFolder",
        "Postmortem. The heron outage lasted nine minutes.",
    )
    run(store_id, source_id)

    assert found(store_id, "heron outage")


# --- Access --------------------------------------------------------------------------------------


def test_access_tokens_are_minted_from_the_refresh_token_when_they_expire(
    api, drive, store_id, synced
):
    run(store_id, synced)
    drive.expire_tokens()
    minted = len(drive.minted)
    drive.edit("1DeployDoc", "Deploying. Canary deploys go to the puffin cluster first.")

    record = run(store_id, synced)

    assert record.status == "succeeded", record.error
    assert len(drive.minted) == minted + 1
    assert found(store_id, "puffin cluster")


def test_a_revoked_grant_needs_reconnecting(api, drive, store_id, synced):
    run(store_id, synced)
    drive.revoke()
    drive.expire_tokens()
    provider.forget_tokens()

    record = run(store_id, synced)

    assert record.status == "failed"
    connector = connector_of(api, connector_of_source(api, synced))
    assert connector["status"] == "needs_reauth"
    assert REFRESH_TOKEN not in (record.error or "")


def test_removing_the_connector_revokes_its_grant_at_google(api, drive, store_id, synced):
    connector_id = connector_of_source(api, synced)

    resp = api.delete(f"/api/connectors/{connector_id}", params={"keep": False})

    assert resp.status_code == 202, resp.text
    assert drive.revoked == [REFRESH_TOKEN]


def test_drive_calls_reach_only_googles_hosts(api, drive, store_id, synced):
    run(store_id, synced)

    hosts = {urlsplit(url).netloc for _, url in drive.requests}

    assert hosts <= {"oauth2.googleapis.com", "www.googleapis.com"}
