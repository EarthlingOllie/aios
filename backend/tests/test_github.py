"""The GitHub connector (`aios.source_providers.github`): an admin installs the aios GitHub App on
chosen repositories, picks from them in aios, and their issues, pull requests and comments (and,
optionally, README and docs Markdown) sync; agents comment and open issues in allowed
repositories only. Against a fake GitHub replaying recorded responses (`fake_github`), through the
API, the sync worker entry point (`aios.sync.run_sync`) and agent turns with the scripted model."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import json
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import store_of
from fake_github import APP_ID, CLIENT_ID, CLIENT_SECRET, CODE, INSTALLATION_ID, SLUG, FakeGitHub
from sqlalchemy import select, text
from test_agents import make_agent
from test_connector_tools import Script, ask, save
from test_connectors import connect as connect_test
from test_connectors import connector_of, stored_text
from test_search import embedded  # noqa: F401  (a fixture)
from test_sync import found, items, offline, run  # noqa: F401  (a fixture)

from aios import connectors
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import SyncKind
from aios.db import SessionLocal
from aios.models import ConnectorCredential, CredentialKind
from aios.source_providers import github as provider
from aios.testing.fake_model import ToolCall, tool_output


@pytest.fixture
def store_id(user, files_bucket) -> uuid.UUID:
    return store_of(user)


@pytest.fixture
def gh(monkeypatch) -> FakeGitHub:
    """GitHub, faked: the App's settings set, and its calls answered by the fake."""
    fake = FakeGitHub()
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", APP_ID)
    monkeypatch.setattr(s, "github_app_slug", SLUG)
    monkeypatch.setattr(s, "github_app_client_id", CLIENT_ID)
    monkeypatch.setattr(
        s, "github_app_private_key", type(s.github_app_private_key)(fake.private_key_pem)
    )
    monkeypatch.setattr(
        s, "github_app_client_secret", type(s.github_app_client_secret)(CLIENT_SECRET)
    )
    monkeypatch.setattr(provider, "TRANSPORT", fake.transport())
    provider.forget_tokens()
    yield fake
    provider.forget_tokens()


def connect(api, gh: FakeGitHub, **override) -> int:
    """Connect GitHub as an admin: aios sends the browser to install the App, and GitHub sends it
    back with the installation and an OAuth code."""
    resp = api.post("/api/connectors", json={"provider": "github"})
    assert resp.status_code == 200, resp.text
    url = urlsplit(resp.json()["redirect_url"])
    assert (url.netloc, url.path) == ("github.com", f"/apps/{SLUG}/installations/new")
    state = parse_qs(url.query)["state"][0]
    query = {
        "installation_id": str(INSTALLATION_ID),
        "setup_action": "install",
        "code": CODE,
        "state": state,
    } | override
    back = api.get("/api/connectors/callback", params=query, follow_redirects=False)
    assert back.status_code == 303, back.text
    return int(back.headers["location"].rsplit("/", 1)[1])


def scoped(api, connector_id: int, repos: list[str], docs: bool = False) -> int:
    resp = api.put(
        f"/api/connectors/{connector_id}/scope",
        json={"items": repos, "acknowledge_restricted": True, "docs": docs},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["source_id"]


# --- Connecting ----------------------------------------------------------------------------------


def test_github_is_unavailable_without_its_app_settings(api, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "github_app_private_key", type(get_settings().github_app_private_key)("")
    )

    listed = {p["provider"]: p for p in api.get("/api/connector-providers").json()}

    assert "app settings" in listed["github"]["unavailable"]


def test_with_its_app_settings_github_can_be_connected(api, gh):
    listed = {p["provider"]: p for p in api.get("/api/connector-providers").json()}

    assert listed["github"]["unavailable"] is None
    assert listed["github"]["title"] == "GitHub"


def test_only_the_installation_id_is_stored_and_tokens_are_minted_when_needed(api, gh, store_id):
    connector_id = connect(api, gh)

    with SessionLocal() as db:
        row = db.scalar(
            select(ConnectorCredential).where(ConnectorCredential.connector_id == connector_id)
        )
        assert row.kind == CredentialKind.github_installation
        assert row.external_account == "acme"
    assert connectors.credential(store_id, connector_id) == str(INSTALLATION_ID)
    # Connecting checked the installation with the person's own GitHub authorisation, and minted
    # no installation token: there was nothing to call yet.
    assert gh.user_tokens and gh.minted == []

    options = api.get(f"/api/connectors/{connector_id}/scope-options")

    assert options.status_code == 200, options.text
    assert len(gh.minted) == 1
    assert {o["id"]: o["restricted"] for o in options.json()} == {
        "acme/api": False,
        "acme/secret-plans": True,
    }


def test_an_installation_the_admin_cant_see_on_github_is_refused(api, gh):
    gh.user_installations = []

    resp = api.post("/api/connectors", json={"provider": "github"})
    state = parse_qs(urlsplit(resp.json()["redirect_url"]).query)["state"][0]
    back = api.get(
        "/api/connectors/callback",
        params={"installation_id": str(INSTALLATION_ID), "code": CODE, "state": state},
        follow_redirects=False,
    )

    assert back.status_code == 400
    assert "GitHub didn't accept" in back.json()["detail"]
    assert api.get("/api/connectors").json() == []


def test_a_bad_oauth_code_is_refused(api, gh):
    resp = api.post("/api/connectors", json={"provider": "github"})
    state = parse_qs(urlsplit(resp.json()["redirect_url"]).query)["state"][0]
    back = api.get(
        "/api/connectors/callback",
        params={"installation_id": str(INSTALLATION_ID), "code": "nope", "state": state},
        follow_redirects=False,
    )

    assert back.status_code == 400


# --- What syncs ----------------------------------------------------------------------------------


def documents(api) -> dict[str, dict]:
    return {d["filename"]: d for d in api.get("/api/documents").json()}


def test_issues_pull_requests_and_their_comments_sync_with_state_labels_author_and_link(
    api, gh, store_id
):
    connector_id = connect(api, gh)

    source_id = scoped(api, connector_id, ["acme/api"])

    synced = items(store_id, source_id)
    assert set(synced) == {
        "acme/api#12",
        "acme/api#13",
        "acme/api#14",
        "acme/api#12/comments/51001",
        "acme/api#13/review-comments/61001",
    }
    issue = synced["acme/api#12"]
    assert issue.title == "acme/api#12: Login fails for SSO users"
    assert issue.url == "https://github.com/acme/api/issues/12"
    assert issue.author_name == "octocat"
    assert "Issue acme/api#12 · state: open · labels: bug, auth" in issue.text
    assert "blank page after the redirect" in issue.text
    pull = synced["acme/api#13"]
    assert "Pull request acme/api#13 · state: merged · labels: enhancement" in pull.text
    comment = synced["acme/api#12/comments/51001"]
    assert comment.parent_id == issue.id and comment.author_name == "monalisa"
    assert comment.url.endswith("#issuecomment-51001")
    review = synced["acme/api#13/review-comments/61001"]
    assert review.parent_id == pull.id
    assert review.title == "Review comment on acme/api#13 (sender/retry.py)"
    assert found(store_id, "Okta tenant") == ["I can reproduce this with the Okta tenant."]
    assert connector_of(api, connector_id)["source"]["health"] == "healthy"


def test_an_incremental_sync_reads_only_whats_updated_since_the_cursor(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    before = items(store_id, source_id)
    gh.put_issue("acme/api", 12, state="closed", labels=["bug"])
    gh.put_comment("acme/api", 13, 61002, "Looks good once the cap is in.", review=True)
    gh.requests.clear()

    record = run(store_id, source_id)

    assert record.status == "succeeded"
    assert (record.added, record.updated) == (1, 1)
    asked = [u for m, u in gh.requests if "/repos/acme/api/issues?" in u]
    assert asked and all("since=2026-10-05T09%3A00%3A00Z" in u for u in asked)
    after = items(store_id, source_id)
    assert "state: closed · labels: bug" in after["acme/api#12"].text
    assert after["acme/api#12"].id == before["acme/api#12"].id
    assert after["acme/api#13/review-comments/61002"].parent_id == after["acme/api#13"].id
    # Unchanged items weren't written again.
    assert after["acme/api#14"].content_hash == before["acme/api#14"].content_hash


def test_listings_are_read_page_by_page(api, gh, store_id):
    gh.page_size = 1
    connector_id = connect(api, gh)

    source_id = scoped(api, connector_id, ["acme/api"])

    assert len(items(store_id, source_id)) == 5


def test_reconciliation_removes_transferred_and_deleted_issues(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.transfer("acme/api", 12)
    gh.delete("acme/api", 14)

    # The change feed doesn't say: they stay until reconciliation.
    run(store_id, source_id)
    assert {"acme/api#12", "acme/api#14"} <= set(items(store_id, source_id))

    record = run(store_id, source_id, SyncKind.full)

    assert record.status == "succeeded"
    assert set(items(store_id, source_id)) == {"acme/api#13", "acme/api#13/review-comments/61001"}
    assert found(store_id, "Okta tenant") == []


def test_reconciliation_removes_deleted_comments(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.delete_comment("acme/api", 51001)

    run(store_id, source_id, SyncKind.full)

    assert "acme/api#12/comments/51001" not in items(store_id, source_id)


def test_a_repository_taken_off_the_installation_is_skipped_and_removed_at_reconciliation(
    api, gh, store_id
):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api", "acme/secret-plans"])
    assert "acme/secret-plans#1" in items(store_id, source_id)
    gh.uninstall("acme/secret-plans")

    assert run(store_id, source_id).status == "succeeded"
    record = run(store_id, source_id, SyncKind.full)

    assert record.status == "succeeded"
    assert not [i for i in items(store_id, source_id) if i.startswith("acme/secret-plans")]
    assert found(store_id, "Narwhal pricing") == []


def test_repositories_added_to_the_installation_later_arent_synced_until_picked(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.install("acme/website")

    run(store_id, source_id, SyncKind.full)

    assert not [i for i in items(store_id, source_id) if i.startswith("acme/website")]
    options = api.get(f"/api/connectors/{connector_id}/scope-options").json()
    assert "acme/website" in [o["id"] for o in options]

    scoped(api, connector_id, ["acme/api", "acme/website"])

    assert "acme/website#3" in items(store_id, source_id)


def test_a_rate_limited_sync_fails_saying_so_and_keeps_its_cursor(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.put_issue("acme/api", 14, title="Typo in the error page, again")
    gh.rate_limit()

    record = run(store_id, source_id)

    assert record.status == "failed"
    assert "rate limit" in record.error
    assert run(store_id, source_id).status == "succeeded"
    assert "again" in items(store_id, source_id)["acme/api#14"].title


def test_an_uninstalled_app_needs_reconnecting(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.revoked = True
    provider.forget_tokens()

    record = run(store_id, source_id)

    assert record.status == "failed"
    assert connector_of(api, connector_id)["status"] == "needs_reauth"


def test_an_expired_token_is_minted_afresh(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    gh.expire_tokens()

    assert run(store_id, source_id).status == "succeeded"
    assert len(gh.minted) == 2


# --- Docs ----------------------------------------------------------------------------------------


def test_docs_syncing_brings_in_readme_and_docs_markdown_and_follows_their_changes(
    api, gh, store_id
):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"])
    assert documents(api) == {}

    resp = api.put(
        f"/api/connectors/{connector_id}/scope",
        json={"items": ["acme/api"], "docs": True},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["docs"] is True
    docs = documents(api)
    assert set(docs) == {"acme/api/README.md", "acme/api/docs/deploying.md"}
    assert {d["source"]["id"] for d in docs.values()} == {source_id}
    assert "blue-green pipeline" in stored_text(store_id, docs["acme/api/docs/deploying.md"]["id"])
    assert any("axolotl tracker" in t for t in found(store_id, "axolotl"))

    # A changed file is re-ingested; a deleted one removed.
    gh.put_file("acme/api", "docs/deploying.md", "# Deploying\n\nDeploys go out on Thursdays.\n")
    gh.delete_file("acme/api", "README.md")
    run(store_id, source_id)

    docs = documents(api)
    assert set(docs) == {"acme/api/docs/deploying.md"}
    assert "Thursdays" in stored_text(store_id, docs["acme/api/docs/deploying.md"]["id"])
    assert found(store_id, "axolotl") == []

    # Turning it off removes them.
    resp = api.put(
        f"/api/connectors/{connector_id}/scope",
        json={"items": ["acme/api"], "docs": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["docs"] is False
    assert documents(api) == {}
    assert "acme/api#12" in items(store_id, source_id)


def test_docs_files_are_reconciled_too(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"], docs=True)
    gh.delete_file("acme/api", "docs/deploying.md")

    run(store_id, source_id, SyncKind.full)

    assert set(documents(api)) == {"acme/api/README.md"}


def test_providers_without_docs_syncing_refuse_the_setting(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "sources_test_provider_enabled", True)
    connector_id = connect_test(api)

    resp = api.put(f"/api/connectors/{connector_id}/scope", json={"items": [], "docs": True})

    assert resp.status_code == 422
    assert connector_of(api, connector_id)["docs"] is None


# --- Removing ------------------------------------------------------------------------------------


def test_removing_with_keep_writes_one_document_per_issue_with_its_comments_and_uninstalls(
    api, gh, store_id
):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"], docs=True)

    resp = api.delete(f"/api/connectors/{connector_id}", params={"keep": True})

    assert resp.status_code == 202, resp.text
    docs = documents(api)
    assert set(docs) == {
        "acme api#12  Login fails for SSO users.md",
        "acme api#13  Add retry to the webhook sender.md",
        "acme api#14  Typo in the error page.md",
        "acme/api/README.md",
        "acme/api/docs/deploying.md",
    }
    assert {d["source"]["name"] for d in docs.values()} == {"Uploads"}
    text = stored_text(store_id, docs["acme api#12  Login fails for SSO users.md"]["id"])
    assert text.startswith("# Login fails for SSO users")
    assert "Issue acme/api#12 · state: open · labels: bug, auth" in text
    assert "**@monalisa**" in text and "Okta tenant" in text
    assert items(store_id, source_id) == {}
    # Its access is revoked at GitHub: the App is uninstalled.
    assert gh.uninstalled == [INSTALLATION_ID]


# --- Credentials ---------------------------------------------------------------------------------


def store_columns_with(store_id, token: str) -> list[str]:
    hits = []
    with store_session(store_id, "rw") as store:
        columns = store.execute(
            text(
                "SELECT table_schema, table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'aios'"
                " AND data_type IN ('text', 'jsonb', 'json', 'character varying', 'ARRAY')"
            )
        ).all()
        for schema, table, column in columns:
            if store.execute(
                text(f'SELECT 1 FROM "{schema}"."{table}" WHERE "{column}"::text LIKE :t LIMIT 1'),
                {"t": f"%{token}%"},
            ).first():
                hits.append(f"{table}.{column}")
    return hits


def test_no_installation_or_user_token_reaches_the_api_or_any_database(api, gh, store_id):
    connector_id = connect(api, gh)
    source_id = scoped(api, connector_id, ["acme/api"], docs=True)
    run(store_id, source_id, SyncKind.full)

    tokens = gh.minted + gh.user_tokens
    assert gh.minted and gh.user_tokens
    responses = [
        api.get("/api/connectors"),
        api.get(f"/api/connectors/{connector_id}"),
        api.get(f"/api/connectors/{connector_id}/tools"),
        api.get(f"/api/sources/{source_id}"),
        api.get("/api/audit-log"),
    ]
    assert all(r.status_code == 200 for r in responses)
    for token in tokens:
        assert not [r.url for r in responses if token in r.text]
        assert store_columns_with(store_id, token) == []
    with SessionLocal() as db:
        rows = db.execute(select(ConnectorCredential.__table__)).all()
        for token in tokens:
            assert not [r for r in rows if token in json.dumps([str(v) for v in r])]


# --- Tools ---------------------------------------------------------------------------------------


def github_pin(connector_id: int, *tools: str, targets: list[str] | None = None) -> dict:
    return {
        "key": "connector",
        "config": {
            "connectors": [
                {
                    "connector_id": connector_id,
                    "provider": "github",
                    "tools": list(tools),
                    "targets": targets or [],
                }
            ]
        },
    }


def reporter(api, connector_id: int, targets: list[str] | None = None) -> dict:
    agent = make_agent(api, "Reporter", base_prompt="Reporter")
    resp = save(
        api, agent, github_pin(connector_id, "comment_on_issue", "open_issue", targets=targets)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def comment(repo: str, number: int, body: str):
    return ToolCall(
        "github__comment_on_issue", {"repository": repo, "number": number, "body": body}
    )


def tool_entries(api) -> list[dict]:
    resp = api.get("/api/audit-log", params={"action": "connector.tool_called"})
    assert resp.status_code == 200, resp.text
    return resp.json()["entries"]


def test_the_agent_editor_gets_githubs_tools_and_a_version_keeps_its_allowed_repositories(api, gh):
    connector_id = connect(api, gh)

    resp = api.get(f"/api/connectors/{connector_id}/tools")

    assert resp.status_code == 200, resp.text
    by_name = {t["name"]: t for t in resp.json()}
    assert set(by_name) == {"comment_on_issue", "open_issue"}
    assert by_name["comment_on_issue"]["input_schema"]["required"] == [
        "repository",
        "number",
        "body",
    ]
    assert not any(t["read_only"] for t in by_name.values())
    agent = reporter(api, connector_id, ["acme/api"])
    [tool] = agent["current_version"]["tools"]
    assert tool["config"]["connectors"][0]["targets"] == ["acme/api"]


def test_an_allowed_comment_is_posted_as_the_app_signed_and_audited(api, gh, user):
    connector_id = connect(api, gh)
    agent = reporter(api, connector_id, ["acme/api"])
    script = Script([comment("acme/api", 12, "The SSO redirect drops the session cookie.")])

    run_ = ask(api, agent, script)

    [posted] = gh.posted
    assert (posted["kind"], posted["repository"], posted["number"]) == ("comment", "acme/api", 12)
    assert posted["body"] == "The SSO redirect drops the session cookie.\n\n— Reporter via aios"
    [result] = script.results
    assert result.status != "error"
    assert "Commented on acme/api#12: https://github.com/acme/api/issues/12#issuecomment-51999" in (
        tool_output(result)
    )
    [entry] = tool_entries(api)
    assert entry["actor"]["agent"] == {"id": agent["id"], "name": "Reporter"}
    assert entry["actor"]["run_id"] == run_["id"]
    details = entry["details"]
    assert (details["provider"], details["tool"], details["target"]) == (
        "github",
        "comment_on_issue",
        "acme/api",
    )
    assert details["outcome"] == "succeeded" and details["caused_by"] == user.id
    assert "session cookie" not in json.dumps(entry)


def test_an_allowed_issue_is_opened_as_the_app_signed_and_audited(api, gh):
    connector_id = connect(api, gh)
    agent = reporter(api, connector_id, ["acme/api"])
    script = Script(
        [
            ToolCall(
                "github__open_issue",
                {"repository": "acme/api", "title": "Flaky export", "body": "It times out."},
            )
        ]
    )

    ask(api, agent, script)

    [posted] = gh.posted
    assert (posted["kind"], posted["title"]) == ("issue", "Flaky export")
    assert posted["body"] == "It times out.\n\n— Reporter via aios"
    assert tool_output(script.results[0]).startswith("Opened acme/api#15: ")
    [entry] = tool_entries(api)
    assert (entry["details"]["tool"], entry["details"]["outcome"]) == ("open_issue", "succeeded")


def test_a_comment_in_a_repository_outside_the_allowed_list_is_refused_and_the_model_told(api, gh):
    connector_id = connect(api, gh)
    agent = reporter(api, connector_id, ["acme/api"])
    script = Script([comment("acme/secret-plans", 1, "Leak it.")])

    ask(api, agent, script)

    assert gh.posted == []
    [result] = script.results
    assert result.status == "error"
    assert tool_output(result) == (
        "The GitHub connector's comment_on_issue may act only on this agent's allowed repositories"
        " (acme/api); acme/secret-plans isn't one of them, so nothing was done."
    )
    [entry] = tool_entries(api)
    assert (entry["details"]["target"], entry["details"]["outcome"]) == (
        "acme/secret-plans",
        "refused",
    )


def test_without_allowed_repositories_every_call_is_refused(api, gh):
    connector_id = connect(api, gh)
    agent = reporter(api, connector_id)
    script = Script([comment("acme/api", 12, "Hello")])

    ask(api, agent, script)

    assert gh.posted == []
    assert tool_output(script.results[0]) == (
        "This agent isn't allowed to act on any repositories with the GitHub connector's"
        " comment_on_issue, so nothing was done."
    )


def test_githubs_refusal_reaches_the_agent_as_an_error_and_is_audited_as_failed(api, gh):
    connector_id = connect(api, gh)
    agent = reporter(api, connector_id, ["acme/api"])
    gh.delete("acme/api", 14)
    script = Script([comment("acme/api", 14, "Still broken?")])

    ask(api, agent, script)

    [result] = script.results
    assert result.status == "error"
    assert "GitHub answered 410" in tool_output(result)
    [entry] = tool_entries(api)
    assert entry["details"]["outcome"] == "failed"
