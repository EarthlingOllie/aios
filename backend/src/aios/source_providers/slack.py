"""The Slack provider: a Slack app with a bot token, got through OAuth (`authorise_url`,
`exchange`, revoked with `revoke`). Every call goes to Slack's Web API through `aios.outbound`, so
it reaches only Slack's allowed hosts; tests replay recorded responses through `TRANSPORT`.

**Scope** is channels the bot can see (`scope_options`): every public channel, and the private
ones it has been invited to (a private channel must invite the app first, as the picker says:
`SCOPE_HINT`). Private channels are restricted: picking one needs the acknowledgement.

**What syncs:** each message, with its channel, author name, time and permalink, as a message
item named `<channel>/<ts>`; a thread reply is `<channel>/<thread ts>/<ts>`, a child of the
thread's first message. Joins, leaves and other channel notices aren't synced, nor are files.

**Re-reading.** A sync reads each channel's history from its cursor, and a window before it
(`slack_reread_days`), with the replies of threads in what it read; the daily reconciliation's
window is longer (`slack_reconcile_days`), and a full resync, or a channel's first sync, reads
everything. What a read covers (`Changes.covers`: the messages, and replies to threads, posted
since its window opened) is settled there: an edit changes the item, and a message gone from it
was deleted. Older messages stay as they are unless an admin runs a full resync. Replies to a
thread older than the window aren't found until a full resync.

**A channel gone** (taken out of the scope, archived, or left by the app) has its items removed
while the other channels carry on, and is marked as needing attention on the connector page
(`Changes.attention`).

**Rate limits** are honoured by slowing down: a 429 waits its Retry-After and tries again, up to
`slack_rate_limit_retries` times in a row, rather than failing the sync.

**Its tool**, `post_to_slack`, is served by aios itself, over MCP, in the process (`MCP`): it
posts as the bot to a channel the agent is allowed (`aios.tools.connector` refuses others), its
text signed "— <agent name> via aios", and answers with the message's permalink.
"""

import hashlib
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from aios import outbound
from aios.config import get_settings
from aios.datastores.models import SyncedItem, SyncedItemKind
from aios.mcp_local import LocalServer, LocalTool
from aios.source_providers import (
    Changes,
    Content,
    Grant,
    Item,
    KeptDocument,
    NeedsReconnecting,
    Provider,
    ScopeOption,
    SourceView,
    Tools,
    kept,
)

log = logging.getLogger(__name__)

NAME = "slack"
API = "https://slack.com/api/"
AUTHORISE = "https://slack.com/oauth/v2/authorize"
# The bot's scopes: public and private channel read and history, user read (for names), and
# chat write; and joining a public channel an admin picks.
SCOPES = (
    "channels:read,channels:history,channels:join,groups:read,groups:history,users:read,chat:write"
)
TOOLS_URL = "https://slack.com/aios-tools/mcp"

SCOPE_HINT = (
    "Only channels the aios app can see are listed: every public channel, and private channels "
    "it has been invited to. To sync a private channel, invite the app to it in Slack first "
    "(/invite @aios), then search again."
)
ARCHIVED = "The channel was archived in Slack, so its messages were removed."
LEFT = (
    "The aios app is no longer in this channel, so its messages were removed. Invite it back in "
    "Slack to sync it again."
)
GONE = (
    "The channel no longer exists in Slack, or the aios app can't see it, so its messages were "
    "removed."
)

# Slack errors meaning the bot's access is gone for good.
REVOKED = {"invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired"}
# Errors reading one channel, meaning the bot can't read it now: the channel needs attention.
CHANNEL_GONE = {"channel_not_found": GONE, "not_in_channel": LEFT, "is_archived": ARCHIVED}

# Message subtypes that are messages people wrote (or bots posted); others are channel notices.
KEPT_SUBTYPES = {None, "thread_broadcast", "bot_message", "me_message", "file_share"}

# Replays recorded responses in tests; None sends calls to Slack.
TRANSPORT: httpx.BaseTransport | None = None
DAY = 24 * 3600


class SlackError(Exception):
    """A Slack call that failed: its error code."""

    def __init__(self, method: str, error: str):
        super().__init__(f"Slack's {method} failed: {error}.")
        self.error = error


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _client(token: str | None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return outbound.client(NAME, transport=TRANSPORT, headers=headers)


def call(
    http: httpx.Client, method: str, params: dict[str, Any] | None = None, *, post: bool = False
) -> dict[str, Any]:
    """Call a Web API method: its answer, if it says `ok`. A 429 waits its Retry-After and tries
    again (`slack_rate_limit_retries` times in a row at most); a revoked token is
    `NeedsReconnecting`; any other error is `SlackError`."""
    settings = get_settings()
    tries = 0
    while True:
        if post:
            resp = http.post(API + method, json=params or {})
        else:
            resp = http.get(API + method, params=params or {})
        if resp.status_code == 429 and tries < settings.slack_rate_limit_retries:
            tries += 1
            wait = float(resp.headers.get("Retry-After") or 1)
            wait = min(max(wait, 0.0), settings.slack_rate_limit_max_wait_seconds)
            log.info("Slack rate-limited %s: waiting %ss", method, wait)
            _sleep(wait)
            continue
        if resp.status_code == 401:
            raise NeedsReconnecting("Slack no longer accepts aios's access.")
        resp.raise_for_status()
        body = resp.json()
        if body.get("ok"):
            return body
        error = str(body.get("error") or "unknown_error")
        if error in REVOKED:
            raise NeedsReconnecting(f"Slack no longer accepts aios's access ({error}).")
        raise SlackError(method, error)


def _pages(
    http: httpx.Client, method: str, params: dict[str, Any], key: str
) -> Iterable[dict[str, Any]]:
    """Every entry of a paginated method's `key`, page by page."""
    cursor = None
    while True:
        body = call(http, method, {**params, **({"cursor": cursor} if cursor else {})})
        yield from body.get(key) or []
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


# --- Reading -----------------------------------------------------------------------------------


def _ts(value: str) -> float:
    return float(value)


def _when(ts: str) -> datetime:
    return datetime.fromtimestamp(_ts(ts), UTC)


def _permalink(base: str, channel: str, ts: str, thread_ts: str | None = None) -> str:
    link = f"{base}archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        link += "?" + urlencode({"thread_ts": thread_ts, "cid": channel})
    return link


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


@dataclass
class _Cursor:
    """Each channel's newest message read (`latest`, a Slack ts), by channel id."""

    latest: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, cursor: str | None) -> "_Cursor":
        if not cursor:
            return cls()
        try:
            data = json.loads(cursor)
        except ValueError:
            return cls()
        return cls(dict(data.get("latest") or {}))

    def dump(self) -> str:
        return json.dumps({"latest": self.latest}, sort_keys=True)


class SlackAdapter:
    """The adapter (`aios.source_providers.Adapter`) over a Slack connector's channels."""

    def __init__(self) -> None:
        self._texts: dict[str, str] = {}
        self._names: dict[str, str] = {}

    # --- scope ---

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        with _client(source.credential) as http:
            channels = _pages(
                http,
                "conversations.list",
                {
                    "types": "public_channel,private_channel",
                    "exclude_archived": "true",
                    "limit": 200,
                },
                "channels",
            )
            return sorted(
                (
                    ScopeOption(c["id"], f"#{c.get('name') or c['id']}", bool(c.get("is_private")))
                    for c in channels
                    if not c.get("is_private") or c.get("is_member")
                ),
                key=lambda o: o.name,
            )

    # --- syncing ---

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        return self._read(source, _Cursor.read(cursor), get_settings().slack_reread_days)

    def listing(self, source: SourceView) -> Changes:
        days = None if source.resync else get_settings().slack_reconcile_days
        return self._read(source, _Cursor.read(None if source.resync else source.cursor), days)

    def fetch(self, source: SourceView, item: Item) -> Content:
        return Content(text=self._texts.get(item.external_id, ""))

    def _read(self, source: SourceView, cursor: _Cursor, days: float | None) -> Changes:
        """Read each channel in scope from its window's start: `days` before its cursor (or now,
        whichever is earlier), or all of it for a channel not read before (or `days` None)."""
        scope = list(source.settings.get("scope") or [])
        now = _now()
        changed: list[Item] = []
        attention: dict[str, str] = {}
        since: dict[str, float] = {}
        latest: dict[str, str] = {}
        with _client(source.credential) as http:
            base = call(http, "auth.test").get("url") or "https://slack.com/"
            if not base.endswith("/"):
                base += "/"
            for channel in scope:
                known = cursor.latest.get(channel)
                start = 0.0 if days is None or known is None else min(_ts(known), now - days * DAY)
                try:
                    info = call(http, "conversations.info", {"channel": channel})["channel"]
                    if info.get("is_archived"):
                        raise SlackError("conversations.info", "is_archived")
                    if not info.get("is_member"):
                        if known is not None or info.get("is_private"):
                            raise SlackError("conversations.info", "not_in_channel")
                        call(http, "conversations.join", {"channel": channel}, post=True)
                    items, newest = self._channel(http, base, channel, info, start)
                except SlackError as exc:
                    if exc.error not in CHANNEL_GONE:
                        raise
                    attention[channel] = CHANNEL_GONE[exc.error]
                    # Everything of it is covered, and nothing returned: its items go.
                    since[channel] = 0.0
                    continue
                changed += items
                since[channel] = start
                latest[channel] = max(
                    [t for t in (known, newest) if t is not None], key=_ts, default=str(now)
                )
        in_scope = set(scope)

        def covers(external_id: str) -> bool:
            channel, _, rest = external_id.partition("/")
            if channel not in in_scope:
                return True
            ts = rest.split("/", 1)[0]
            try:
                return channel in since and _ts(ts) >= since[channel]
            except ValueError:
                return False

        return Changes(changed, [], _Cursor(latest).dump(), covers=covers, attention=attention)

    def _channel(
        self, http: httpx.Client, base: str, channel: str, info: dict[str, Any], start: float
    ) -> tuple[list[Item], str | None]:
        """The channel's messages posted since `start`, with the replies of their threads, and
        the newest ts read."""
        name = f"#{info.get('name') or channel}"
        params: dict[str, Any] = {"channel": channel, "limit": 200}
        if start > 0:
            params["oldest"] = f"{start:.6f}"
            params["inclusive"] = "true"
        items: list[Item] = []
        newest: str | None = None
        for message in _pages(http, "conversations.history", params, "messages"):
            if message.get("subtype") not in KEPT_SUBTYPES or "ts" not in message:
                continue
            ts = message["ts"]
            if message.get("thread_ts", ts) != ts:
                continue  # a reply also sent to the channel: synced with its thread
            if newest is None or _ts(ts) > _ts(newest):
                newest = ts
            items.append(self._item(http, base, channel, name, message))
            if message.get("reply_count") and message.get("thread_ts", ts) == ts:
                thread = {"channel": channel, "ts": ts, "limit": 200}
                for reply in _pages(http, "conversations.replies", thread, "messages"):
                    if reply.get("ts") == ts or reply.get("subtype") not in KEPT_SUBTYPES:
                        continue
                    if _ts(reply["ts"]) > _ts(newest):
                        newest = reply["ts"]
                    items.append(self._item(http, base, channel, name, reply, thread_ts=ts))
        return items, newest

    def _author(self, http: httpx.Client, message: dict[str, Any]) -> str:
        if message.get("user_profile"):
            p = message["user_profile"]
            found = p.get("display_name") or p.get("real_name")
            if found:
                return found
        user = message.get("user")
        if user:
            if user not in self._names:
                try:
                    u = call(http, "users.info", {"user": user})["user"]
                    p = u.get("profile") or {}
                    self._names[user] = (
                        p.get("display_name")
                        or p.get("real_name")
                        or u.get("real_name")
                        or u.get("name")
                        or user
                    )
                except SlackError:
                    self._names[user] = user
            return self._names[user]
        return (message.get("bot_profile") or {}).get("name") or message.get("username") or "Slack"

    def _item(
        self,
        http: httpx.Client,
        base: str,
        channel: str,
        name: str,
        message: dict[str, Any],
        thread_ts: str | None = None,
    ) -> Item:
        ts = message["ts"]
        text = message.get("text") or ""
        author = self._author(http, message)
        edited = (message.get("edited") or {}).get("ts") or ""
        external_id = f"{channel}/{thread_ts}/{ts}" if thread_ts else f"{channel}/{ts}"
        self._texts[external_id] = text
        when = _when(ts)
        return Item(
            external_id=external_id,
            kind=SyncedItemKind.message,
            title=f"{name}, {when:%Y-%m-%d %H:%M} UTC",
            content_hash=_hash(text, author, edited),
            url=_permalink(base, channel, ts, thread_ts),
            author_name=author,
            updated_at=when,
            parent_external_id=f"{channel}/{thread_ts}" if thread_ts else None,
        )


# --- Connecting --------------------------------------------------------------------------------


def _redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/api/connectors/callback"


def _authorise_url(state: str) -> str:
    s = get_settings()
    return (
        AUTHORISE
        + "?"
        + urlencode(
            {
                "client_id": s.slack_client_id,
                "scope": SCOPES,
                "state": state,
                "redirect_uri": _redirect_uri(),
            }
        )
    )


def _exchange(code: str) -> Grant:
    s = get_settings()
    with outbound.client(NAME, transport=TRANSPORT) as http:
        resp = http.post(
            API + "oauth.v2.access",
            data={
                "code": code,
                "redirect_uri": _redirect_uri(),
                "client_id": s.slack_client_id,
                "client_secret": s.slack_client_secret.get_secret_value(),
            },
        )
        resp.raise_for_status()
        body = resp.json()
    if not body.get("ok") or not body.get("access_token"):
        raise ValueError(f"Slack didn't accept the code: {body.get('error')}.")
    team = body.get("team") or {}
    return Grant(
        secret=body["access_token"],
        kind="oauth",
        scopes=body.get("scope"),
        external_account=team.get("name") or team.get("id"),
    )


def _revoke(secret: str) -> None:
    with _client(secret) as http:
        call(http, "auth.revoke")


def _keep_groups(rows: Iterable[SyncedItem]) -> list[KeptDocument]:
    """One Markdown document per channel per month, named by the channel's name."""
    return kept.by_channel_and_month(rows, _channel_name)


def _channel_name(row: SyncedItem) -> str:
    # An item's title is "#<channel>, <time>".
    if row.title and row.title.startswith("#") and ", " in row.title:
        return row.title.split(", ", 1)[0]
    return row.external_id.split("/", 1)[0]


# --- Its tool ----------------------------------------------------------------------------------

POST = "post_to_slack"
SIGNATURE = "\n\n— {agent} via aios"


def _post(arguments: dict[str, Any], token: str | None) -> str:
    channel = str(arguments.get("channel") or "").strip()
    text = str(arguments.get("text") or "")
    if not channel or not text.strip():
        raise ValueError("post_to_slack needs a channel and some text.")
    with _client(token) as http:
        try:
            posted = call(http, "chat.postMessage", {"channel": channel, "text": text}, post=True)
            link = call(
                http,
                "chat.getPermalink",
                {"channel": posted.get("channel") or channel, "message_ts": posted["ts"]},
            )["permalink"]
        except SlackError as exc:
            raise ValueError(f"Slack refused the post: {exc.error}.") from exc
        except NeedsReconnecting as exc:
            raise ValueError(
                "Slack no longer accepts aios's access: an admin must reconnect it."
            ) from exc
    return f"Posted to {channel}: {link}"


MCP = LocalServer(
    "aios-slack",
    [
        LocalTool(
            POST,
            "Post a message to a Slack channel, as the aios app. Only the channels you're allowed "
            "can be posted to. Answers with the message's link.",
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "The channel's id (such as C0123456789).",
                    },
                    "text": {"type": "string", "description": "The message, in Slack's markup."},
                },
                "required": ["channel", "text"],
            },
            reply=_post,
        )
    ],
)


def _unavailable() -> str | None:
    s = get_settings()
    if s.slack_client_id and s.slack_client_secret.get_secret_value():
        return None
    return "Slack isn't set up in this deployment: it needs its app settings."


PROVIDER = Provider(
    NAME,
    "Slack",
    _unavailable,
    adapter=SlackAdapter,
    authorise_url=_authorise_url,
    exchange=_exchange,
    revoke=_revoke,
    keep_groups=_keep_groups,
    tools=Tools(
        TOOLS_URL,
        transport=MCP.transport,
        target_args=("channel",),
        limited={POST: "channel"},
        signed={POST: "text"},
    ),
    scope_hint=SCOPE_HINT,
)
