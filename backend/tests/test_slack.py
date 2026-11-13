"""Slack (`aios.source_providers.slack`): connecting, picking channels, syncing messages and
thread replies with re-read windows and a full resync, channels gone, rate limits, and the
`post_to_slack` tool. Slack is never called: its Web API is replayed from recorded responses
(`recordings/slack/`, `recorded.Replay`). Seen through the API, the sync worker entry point
(`aios.sync.run_sync`), and what the scripted model is given and gets back."""

# The fixtures imported from other test files are used as arguments, which ruff takes for
# redefinitions.
# ruff: noqa: F811

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from recorded import Replay, load
from sqlalchemy import text
from test_agents import make_agent
from test_chats import add, make_chat, messages  # noqa: F401
from test_connector_tools import Script, ask, save, tool_entries
from test_connectors import connector_of, editor, scoped, set_scope, start  # noqa: F401
from test_guards import participant  # noqa: F401  (a fixture)
from test_sync import items, offline, run  # noqa: F401  (a fixture)

from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import SyncKind
from aios.source_providers import slack
from aios.testing.fake_model import ToolCall, tool_output
from aios.tools import connector as connector_tools

NOW = 1800000000.0
TOKEN = "xoxb-recorded-bot-token"
GENERAL, PLANS = "C0GENERAL", "G0PLANS"
M1, M2, R1, M3 = "1791360000.000001", "1797408000.000002", "1799827200.000003", "1799913600.000030"
M4, M5, M6, P1 = "1799956800.000004", "1799568000.000005", "1798272000.000006", "1799740800.000007"


class Slack:
    """Slack as the tests see it: the recorded responses it answers with now, and the waits a
    rate limit made."""

    def __init__(self) -> None:
        self.replay = Replay()
        self.waits: list[float] = []

    def answers(self, *names: str) -> Replay:
        """Answer from these recordings from now on."""
        self.replay = Replay(*[e for name in names for e in load(f"slack/{name}.json")])
        slack.TRANSPORT = self.replay.transport()
        return self.replay


@pytest.fixture
def workspace(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "slack_client_id", "1234.5678")
    monkeypatch.setattr(s, "slack_client_secret", type(s.slack_client_secret)("app-secret"))
    fake = Slack()
    monkeypatch.setattr(slack, "_now", lambda: NOW)
    monkeypatch.setattr(slack, "_sleep", fake.waits.append)
    connector_tools.forget_listings()
    yield fake
    slack.TRANSPORT = None
    connector_tools.forget_listings()


@pytest.fixture
def store_id(user, files_bucket):
    from conftest import store_of

    return store_of(user)


def connect(api, workspace: Slack) -> int:
    resp = start(api, "slack")
    assert resp.status_code == 200, resp.text
    url = urlsplit(resp.json()["redirect_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.netloc, url.path) == ("slack.com", "/oauth/v2/authorize")
    workspace.answers("oauth")
    back = api.get(
        "/api/connectors/callback",
        params={"state": query["state"], "code": "slack-code"},
        follow_redirects=False,
    )
    assert back.status_code == 303, back.text
    return int(back.headers["location"].rsplit("/", 1)[1])


def synced(api, workspace: Slack, *recordings: str) -> tuple[int, int]:
    """A connected Slack syncing #general and #plans, first synced from `recordings`: its
    connector's and source's ids."""
    connector_id = connect(api, workspace)
    workspace.answers("channels", *(recordings or ("first",)))
    source_id = scoped(api, connector_id, [GENERAL, PLANS])
    return connector_id, source_id


def texts(store_id, source_id) -> dict[str, str]:
    return {k: v.text for k, v in items(store_id, source_id).items()}


def latest_sync(api, source_id: int) -> dict:
    return api.get(f"/api/sources/{source_id}").json()["last_sync"]


# --- Connecting and picking channels -----------------------------------------------------------


def test_an_admin_connects_slack_through_oauth_and_the_token_is_kept_out_of_sight(api, workspace):
    resp = start(api, "slack")
    query = parse_qs(urlsplit(resp.json()["redirect_url"]).query)
    assert query["client_id"] == ["1234.5678"]
    assert set(query["scope"][0].split(",")) >= {
        "channels:read",
        "channels:history",
        "groups:read",
        "groups:history",
        "users:read",
        "chat:write",
    }

    connector_id = connect(api, workspace)

    [exchange] = workspace.replay.calls("/api/oauth.v2.access")
    assert exchange["code"] == "slack-code" and exchange["client_secret"] == "app-secret"
    connector = connector_of(api, connector_id)
    assert (connector["provider"], connector["status"]) == ("slack", "active")
    assert TOKEN not in json.dumps(connector)


def test_the_picker_lists_the_channels_the_app_can_see_and_says_private_ones_must_invite_it(
    api, workspace
):
    connector_id = connect(api, workspace)
    workspace.answers("channels")

    options = api.get(f"/api/connectors/{connector_id}/scope-options").json()

    assert options == [
        {"id": GENERAL, "name": "#general", "restricted": False},
        {"id": PLANS, "name": "#plans", "restricted": True},
        {"id": "C0RANDOM", "name": "#random", "restricted": False},
    ]
    hint = connector_of(api, connector_id)["scope_hint"]
    assert "private channel" in hint.lower() and "invite" in hint.lower()
    # Called with the bot's token.
    [listing] = [r for r in workspace.replay.requests if r.url.path.endswith("conversations.list")]
    assert listing.headers["authorization"] == f"Bearer {TOKEN}"


def test_a_private_channel_needs_the_acknowledgement(api, workspace):
    connector_id = connect(api, workspace)
    workspace.answers("channels")

    resp = set_scope(api, connector_id, [PLANS])

    assert resp.status_code == 422


# --- Syncing -----------------------------------------------------------------------------------


def test_messages_and_thread_replies_sync_with_their_channel_author_and_permalink(
    api, store_id, workspace
):
    _, source_id = synced(api, workspace)

    synced_items = items(store_id, source_id)
    assert set(synced_items) == {f"{GENERAL}/{m}" for m in (M1, M6, M2, M5, M3)} | {
        f"{GENERAL}/{M2}/{R1}",
        f"{PLANS}/{P1}",
    }
    reply = synced_items[f"{GENERAL}/{M2}/{R1}"]
    assert reply.text == "Brass, polished."
    assert reply.author_name == "Bob Baker"
    assert reply.parent_id == synced_items[f"{GENERAL}/{M2}"].id
    assert reply.url == (
        f"https://acme.slack.com/archives/{GENERAL}/p1799827200000003?thread_ts={M2}&cid={GENERAL}"
    )
    first = synced_items[f"{GENERAL}/{M2}"]
    assert first.author_name == "Ann"
    assert first.url == f"https://acme.slack.com/archives/{GENERAL}/p1797408000000002"
    assert first.title == "#general, 2026-12-16 08:00 UTC"
    assert synced_items[f"{PLANS}/{P1}"].title.startswith("#plans, ")
    # A channel join isn't a message anyone wrote; history came over two pages.
    assert len(workspace.replay.calls("/api/conversations.history")) == 3
    assert workspace.replay.unmatched == []


def test_each_sync_rereads_the_recent_window_catching_edits_and_deletions_inside_it_only(
    api, store_id, workspace
):
    _, source_id = synced(api, workspace)
    replay = workspace.answers("incremental")

    record = run(store_id, source_id)

    assert record.status == "succeeded", record.error
    [general, _] = replay.calls("/api/conversations.history")
    assert float(general["oldest"]) == NOW - 7 * 86400
    now = texts(store_id, source_id)
    assert now[f"{GENERAL}/{M3}"] == "The orrery's Saturn has rings and moons now."  # edited
    assert now[f"{GENERAL}/{M4}"] == "Orrery demo at noon."  # new
    assert f"{GENERAL}/{M5}" not in now  # deleted inside the window
    # Outside the window: deleted (m1) and edited (m6) at Slack, settled here for now; and an
    # older thread's reply stays.
    assert now[f"{GENERAL}/{M1}"] == "Kickoff: we're building the orrery."
    assert now[f"{GENERAL}/{M6}"] == "The orrery's gears arrive Friday."
    assert f"{GENERAL}/{M2}/{R1}" in now
    assert (record.added, record.updated, record.removed) == (1, 1, 1)
    assert replay.unmatched == []


def test_the_daily_reconciliation_rereads_the_longer_window(api, store_id, workspace):
    _, source_id = synced(api, workspace)
    workspace.answers("incremental")
    run(store_id, source_id)
    replay = workspace.answers("reconcile")

    # Reconciliation is due (as it is daily).
    with store_session(store_id, "rw") as store:
        store.execute(
            text("UPDATE sources SET last_reconciled_at = now() - interval '2 days' WHERE id = :i"),
            {"i": source_id},
        )
        store.commit()
    record = run(store_id, source_id)

    assert record.status == "succeeded", record.error
    assert record.kind == "full"
    [general, _] = replay.calls("/api/conversations.history")
    assert float(general["oldest"]) == NOW - 90 * 86400
    now = texts(store_id, source_id)
    assert now[f"{GENERAL}/{M2}"] == "Which metal for the orrery's planets and moons?"
    assert f"{GENERAL}/{M6}" not in now  # deleted 20 days ago: caught
    assert f"{GENERAL}/{M1}" in now  # 100 days old: still settled
    assert replay.unmatched == []


def test_a_full_resync_rereads_everything(api, store_id, workspace):
    _, source_id = synced(api, workspace)
    workspace.answers("incremental")
    run(store_id, source_id)
    replay = workspace.answers("resync")

    record = run(store_id, source_id, SyncKind.full)

    assert record.status == "succeeded", record.error
    assert all("oldest" not in c for c in replay.calls("/api/conversations.history"))
    assert set(texts(store_id, source_id)) == {
        f"{GENERAL}/{M4}",
        f"{GENERAL}/{M3}",
        f"{GENERAL}/{M2}",
        f"{GENERAL}/{M2}/{R1}",
        f"{PLANS}/{P1}",
    }
    assert replay.unmatched == []


@pytest.mark.parametrize(
    "recording, reason",
    [("archived", slack.ARCHIVED), ("left", slack.LEFT), ("missing", slack.GONE)],
)
def test_a_channel_archived_or_left_by_the_app_loses_its_items_and_needs_attention(
    api, store_id, workspace, recording, reason
):
    connector_id, source_id = synced(api, workspace)
    replay = workspace.answers(recording)

    record = run(store_id, source_id)

    assert record.status == "succeeded", record.error
    assert set(texts(store_id, source_id)) == {f"{PLANS}/{P1}"}  # the other carries on
    assert connector_of(api, connector_id)["attention"] == [
        {"id": GENERAL, "name": "#general", "reason": reason}
    ]
    assert replay.calls("/api/conversations.join") == []  # left: not joined again
    assert replay.unmatched == []


def test_a_channel_back_to_normal_no_longer_needs_attention(api, store_id, workspace):
    connector_id, source_id = synced(api, workspace)
    workspace.answers("archived")
    run(store_id, source_id)
    # Its items went, so it's read afresh.
    workspace.answers("resync")

    run(store_id, source_id)

    assert connector_of(api, connector_id)["attention"] == []


def test_a_channel_taken_out_of_the_scope_loses_its_items(api, store_id, workspace):
    connector_id, source_id = synced(api, workspace)
    replay = workspace.answers("channels", "resync")

    resp = set_scope(api, connector_id, [PLANS], acknowledge=True)

    assert resp.status_code == 200, resp.text
    assert set(texts(store_id, source_id)) == {f"{PLANS}/{P1}"}
    assert replay.calls("/api/conversations.info") == [{"channel": PLANS}]


def test_a_rate_limited_response_slows_the_sync_instead_of_failing_it(api, store_id, workspace):
    _, source_id = synced(api, workspace, "rate_limited")

    assert latest_sync(api, source_id)["status"] == "succeeded"
    assert workspace.waits == [3.0, 3.0]
    assert f"{GENERAL}/{M3}" in texts(store_id, source_id)


def test_a_revoked_token_needs_reconnecting(api, store_id, workspace):
    connector_id, source_id = synced(api, workspace)
    workspace.answers()
    workspace.replay.add(
        {"path": "/api/auth.test", "json": {"ok": False, "error": "token_revoked"}}
    )

    record = run(store_id, source_id)

    assert record.status == "failed"
    assert connector_of(api, connector_id)["status"] == "needs_reauth"


def test_the_connectors_health_shows_on_the_data_page(api, workspace):
    connector_id, source_id = synced(api, workspace)

    [connector] = api.get("/api/connectors").json()

    assert connector["id"] == connector_id
    assert connector["source"]["health"] == "healthy"


def test_the_token_is_never_in_the_api_or_the_store(api, store_id, workspace):
    connector_id, source_id = synced(api, workspace)
    responses = [
        api.get("/api/connectors"),
        api.get(f"/api/connectors/{connector_id}"),
        api.get(f"/api/sources/{source_id}"),
        api.get("/api/audit-log"),
    ]
    assert not any(TOKEN in r.text for r in responses)
    with store_session(store_id, "ro") as store:
        columns = store.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'aios'"
                " AND data_type IN ('text', 'jsonb', 'character varying')"
            )
        ).all()
        for table, column in columns:
            hit = store.execute(
                text(f'SELECT 1 FROM aios."{table}" WHERE "{column}"::text LIKE :t LIMIT 1'),
                {"t": f"%{TOKEN}%"},
            ).first()
            assert hit is None, f"{table}.{column}"


# --- Posting -----------------------------------------------------------------------------------


def poster(api, connector_id: int, allowed: list[str], name: str = "Herald") -> dict:
    agent = make_agent(api, name, base_prompt=name)
    resp = save(
        api,
        agent,
        {
            "key": "connector",
            "config": {
                "connectors": [
                    {
                        "connector_id": connector_id,
                        "provider": "slack",
                        "tools": ["post_to_slack"],
                        "allowed": {"post_to_slack": allowed},
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def post(channel: str, said: str = "The orrery is finished.") -> ToolCall:
    return ToolCall("slack__post_to_slack", {"channel": channel, "text": said})


def test_the_editor_sees_the_post_tool_limited_to_channels_it_picks(api, workspace):
    connector_id = connect(api, workspace)
    workspace.answers("channels")

    [tool] = api.get(f"/api/connectors/{connector_id}/tools").json()

    assert tool["name"] == "post_to_slack" and not tool["read_only"]
    assert tool["limited_by"] == "channel"
    assert [t["id"] for t in tool["targets"]] == [GENERAL, PLANS, "C0RANDOM"]


def test_the_post_tool_needs_a_channel_it_may_post_to(api, workspace):
    connector_id = connect(api, workspace)
    agent = make_agent(api, "Herald")

    resp = save(
        api,
        agent,
        {
            "key": "connector",
            "config": {
                "connectors": [
                    {"connector_id": connector_id, "provider": "slack", "tools": ["post_to_slack"]}
                ]
            },
        },
    )

    assert resp.status_code == 422
    assert "post_to_slack needs at least one channel it may use" in resp.json()["detail"]


def test_posting_outside_the_allowed_channels_is_refused_and_the_model_is_told(api, workspace):
    connector_id = connect(api, workspace)
    agent = poster(api, connector_id, [GENERAL])
    replay = workspace.answers("post")
    script = Script([post(PLANS)])

    ask(api, agent, script)

    [result] = script.results
    assert result.status == "error"
    assert tool_output(result) == (
        f"post_to_slack may act only on the channels it's allowed ({GENERAL}); {PLANS} isn't one"
        " of them, so nothing was done."
    )
    assert replay.calls("/api/chat.postMessage") == []
    [entry] = tool_entries(api)
    assert entry["details"]["outcome"] == "refused"
    assert entry["details"]["target"] == PLANS
    assert f"Allowed channels: {GENERAL}." in script.tools[0]["slack__post_to_slack"].description


def test_an_allowed_post_is_made_as_the_bot_signed_and_audited_and_returns_its_permalink(
    api, workspace, user
):
    connector_id = connect(api, workspace)
    agent = poster(api, connector_id, [GENERAL])
    replay = workspace.answers("post")
    script = Script([post(GENERAL)])

    run_ = ask(api, agent, script)

    [posted] = replay.calls("/api/chat.postMessage")
    assert posted == {
        "channel": GENERAL,
        "text": "The orrery is finished.\n\n— Herald via aios",
    }
    [request] = [r for r in replay.requests if r.url.path.endswith("chat.postMessage")]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    [result] = script.results
    assert result.status != "error"
    assert tool_output(result) == (
        f"Posted to {GENERAL}: https://acme.slack.com/archives/{GENERAL}/p1800000001000100"
    )
    [entry] = tool_entries(api)
    assert entry["actor"]["agent"] == {"id": agent["id"], "name": "Herald"}
    details = entry["details"]
    assert (details["tool"], details["target"], details["outcome"]) == (
        "post_to_slack",
        GENERAL,
        "succeeded",
    )
    assert details["turn_id"] == run_["turns"][0]["id"]
    assert details["caused_by"] == user.id
    assert "orrery" not in json.dumps(entry)
