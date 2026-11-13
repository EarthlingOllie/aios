"""The test provider: a source fed from an in-process feed (`Feed`), for development and tests
only (`sources_test_provider_enabled`). A source names its feed in its settings (`feed`); one not
registered with `feed` gets the sample feed's items.

A feed is a change log: putting or deleting an item adds a change, and a cursor is how many
changes have been read. A test can also delete an item without the change feed saying
(`delete(..., silently=True)`), so only a full listing catches it; make fetching fail after so
many items (`fail_after`), or for good (`broken`); or take its access away (`revoked`).

**As a connector**, every feed is a channel an admin can pick (`scope_options`; a feed marked
`restricted` is private at its source), and a connector's source syncs the feeds in its scope,
its items' external ids prefixed with their feed's name. Connecting needs no third party: the
authorisation "page" sends the browser straight back to the callback with a code, exchanged for a
fresh token. Each sync makes the calls its feeds list (`calls`) through `aios.outbound`, so they
reach only the provider's allowed hosts (served in-process, never sent); a feed notes the tokens it
was called with (`tokens`), and revoked tokens are noted in `REVOKED`. A removed connector's
message items are kept as one Markdown document per feed (channel) per month.

**Its tools** are served by a fake MCP server in the process (`MCP`, `aios.testing.fake_mcp`), at
`https://test.invalid/mcp`: `post_message` posts a message to a feed (a side effect), and
`read_channel` reads one (read-only). Tests change them (add, remove, make one fail);
`reset_tools` puts them back as they start.

**Its pages** stand in for web pages a URL source reads (`aios.url_sources`): `page` puts one at
`https://test.invalid/pages/<path>`, which `aios.url_fetch` answers in the process (`serve`)
while the test provider is on; `remove_page` makes it "not found".

**Shared** (`sources_test_feeds_shared`, the full-stack test's stack only), feeds and pages are
kept in Redis too, so the stack's processes see one another's changes: each read loads them
(`load`, `_pull`), and each change is saved (`save`), whoever makes it (the testing routes,
`aios.testing.feeds_api`, or the `post_message` tool). Otherwise they live in the process, as
the backend tests use them.
"""

import base64
import hashlib
import json
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import httpx
import redis

from aios import outbound
from aios.config import get_settings
from aios.datastores.models import SyncedItem, SyncedItemKind
from aios.source_providers import (
    TEST,
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
    _test_unavailable,
    kept,
)
from aios.testing.fake_mcp import FakeServer, FakeTool


class FeedBroken(Exception):
    """The test feed failing, as a provider's outage would."""


@dataclass
class _Entry:
    item: Item
    content: Content


@dataclass
class Feed:
    name: str
    items: dict[str, _Entry] = field(default_factory=dict)
    # (external id, removed?) in order.
    log: list[tuple[str, bool]] = field(default_factory=list)
    # Fetches left before fetching fails (None: never).
    fail_after: int | None = None
    broken: bool = False
    revoked: bool = False
    fetched: list[str] = field(default_factory=list)
    # Private at its source: picking it needs an acknowledgement.
    restricted: bool = False
    # URLs each sync of it calls (through `aios.outbound`), and the tokens it was called with.
    calls: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def message(self, external_id: str, text: str, title: str | None = None, **extra) -> Item:
        """Put a message item (new or changed)."""
        item = Item(
            external_id=external_id,
            kind=SyncedItemKind.message,
            title=title or external_id,
            content_hash=_hash(text.encode()),
            url=f"https://test.invalid/{self.name}/{external_id}",
            **extra,
        )
        return self._put(item, Content(text=text))

    def file(
        self,
        external_id: str,
        data: bytes,
        filename: str,
        content_type: str = "text/markdown",
        **extra,
    ) -> Item:
        """Put a file item (new or changed)."""
        item = Item(
            external_id=external_id,
            kind=SyncedItemKind.file,
            title=filename,
            content_hash=_hash(data),
            url=f"https://test.invalid/{self.name}/{external_id}",
            content_type=content_type,
            **extra,
        )
        return self._put(item, Content(data=data))

    def _put(self, item: Item, content: Content) -> Item:
        self.items[item.external_id] = _Entry(item, content)
        self.log.append((item.external_id, False))
        return item

    def delete(self, external_id: str, *, silently: bool = False) -> None:
        """Delete an item; `silently`, the change feed doesn't say (only a listing shows it)."""
        self.items.pop(external_id, None)
        if not silently:
            self.log.append((external_id, True))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FEEDS: dict[str, Feed] = {}
# The pages, by path under `PAGES_URL`: (body, content type).
PAGES: dict[str, tuple[bytes, str]] = {}
PAGES_URL = "https://test.invalid/pages/"
HOST = "test.invalid"

# Where shared feeds and pages are kept, in Redis: hashes by name and by path.
_FEEDS_KEY = "aios:test-provider:feeds"
_PAGES_KEY = "aios:test-provider:pages"


def feed(name: str) -> Feed:
    """The feed called `name`, made empty if there's none."""
    _pull()
    return FEEDS.setdefault(name, Feed(name))


def forget(name: str) -> None:
    FEEDS.pop(name, None)
    if _shared():
        with _redis() as client:
            client.hdel(_FEEDS_KEY, name)


# --- Shared through Redis -----------------------------------------------------------------------


def _shared() -> bool:
    return get_settings().sources_test_feeds_shared


def _redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)


def _dumped(f: Feed) -> str:
    def entry(e: _Entry) -> dict:
        item = asdict(e.item)
        item["kind"] = e.item.kind.value
        item["updated_at"] = e.item.updated_at.isoformat() if e.item.updated_at else None
        data = e.content.data
        return {
            "item": item,
            "text": e.content.text,
            "data": None if data is None else base64.b64encode(data).decode(),
        }

    return json.dumps(
        {
            "name": f.name,
            "items": [entry(e) for e in f.items.values()],
            "log": f.log,
            "restricted": f.restricted,
            "broken": f.broken,
            "revoked": f.revoked,
        }
    )


def _loaded(raw: str | bytes) -> Feed:
    found = json.loads(raw)
    f = Feed(
        found["name"],
        restricted=found["restricted"],
        broken=found["broken"],
        revoked=found["revoked"],
        log=[(external_id, gone) for external_id, gone in found["log"]],
    )
    for e in found["items"]:
        item = dict(e["item"])
        item["kind"] = SyncedItemKind(item["kind"])
        if item["updated_at"]:
            item["updated_at"] = datetime.fromisoformat(item["updated_at"])
        data = None if e["data"] is None else base64.b64decode(e["data"])
        f.items[item["external_id"]] = _Entry(Item(**item), Content(text=e["text"], data=data))
    return f


def save(name: str) -> None:
    """Keep the feed's changes where the stack's other processes see them (shared only)."""
    if not _shared() or name not in FEEDS:
        return
    with _redis() as client:
        client.hset(_FEEDS_KEY, name, _dumped(FEEDS[name]))


def _pull() -> None:
    """Take in the shared feeds and pages, as last saved (shared only)."""
    if not _shared():
        return
    with _redis() as client:
        feeds = client.hgetall(_FEEDS_KEY)
        pages = client.hgetall(_PAGES_KEY)
    for raw in feeds.values():
        f = _loaded(raw)
        FEEDS[f.name] = f
    # Pages as saved, and only those: one taken away elsewhere is gone here too.
    PAGES.clear()
    for path, raw in pages.items():
        found = json.loads(raw)
        PAGES[path.decode()] = (base64.b64decode(found["body"]), found["content_type"])


def load(name: str) -> Feed | None:
    """The feed called `name` as it is now (shared: as last saved), if there is one."""
    _pull()
    return FEEDS.get(name)


# --- Pages --------------------------------------------------------------------------------------


def page(path: str, text: str, content_type: str = "text/markdown") -> None:
    """Put a page at `PAGES_URL` + `path` (new or changed)."""
    PAGES[path] = (text.encode(), content_type)
    if _shared():
        body = base64.b64encode(text.encode()).decode()
        with _redis() as client:
            client.hset(_PAGES_KEY, path, json.dumps({"body": body, "content_type": content_type}))


def remove_page(path: str) -> None:
    """Take the page away: it's "not found" from now."""
    PAGES.pop(path, None)
    if _shared():
        with _redis() as client:
            client.hdel(_PAGES_KEY, path)


def serve(request: httpx.Request) -> httpx.Response:
    """Answer a request for a page, as its web server would: "not found" for one there isn't,
    "not modified" for one the request's ETag matches."""
    path, prefix = request.url.path, httpx.URL(PAGES_URL).path
    _pull()
    found = PAGES.get(path.removeprefix(prefix)) if path.startswith(prefix) else None
    if found is None:
        return httpx.Response(404, request=request)
    body, content_type = found
    etag = f'"{_hash(body)[:16]}"'
    if request.headers.get("if-none-match") == etag:
        return httpx.Response(304, request=request)
    return httpx.Response(
        200, content=body, headers={"content-type": content_type, "etag": etag}, request=request
    )


def _sample() -> Feed:
    sample = Feed("sample")
    sample.message(
        "welcome",
        "Welcome to the test provider. Its items are synced like any source's.",
        title="Welcome",
    )
    sample.message(
        "zebrafish",
        "Zebrafish embryos are transparent, which makes them useful for developmental biology.",
        title="Zebrafish",
    )
    return sample


def _feed_of(source: SourceView) -> Feed:
    _pull()
    name = source.settings.get("feed") or "sample"
    if name not in FEEDS and name == "sample":
        FEEDS[name] = _sample()
    return feed(name)


# The tokens revoked at the test provider (a removed connector's).
REVOKED: list[str] = []

# Calls to the test provider's own host are answered here: nothing is sent.
_SERVED = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))


def _scoped(source: SourceView) -> list[Feed] | None:
    """A connector's source: the feeds in its scope (None for a source on one feed)."""
    scope = source.settings.get("scope")
    if scope is None:
        return None
    _pull()
    return [FEEDS[name] for name in scope if name in FEEDS]


def _prefixed(f: Feed, item: Item) -> Item:
    parent = item.parent_external_id
    return Item(
        external_id=f"{f.name}/{item.external_id}",
        kind=item.kind,
        title=item.title,
        content_hash=item.content_hash,
        url=item.url,
        author_name=item.author_name,
        updated_at=item.updated_at,
        parent_external_id=None if parent is None else f"{f.name}/{parent}",
        content_type=item.content_type,
    )


def _changes_in(f: Feed, start: int) -> Changes:
    changed: dict[str, Item] = {}
    removed: set[str] = set()
    for external_id, gone in f.log[start:]:
        if gone or external_id not in f.items:
            changed.pop(external_id, None)
            removed.add(external_id)
        else:
            removed.discard(external_id)
            changed[external_id] = f.items[external_id].item
    return Changes(list(changed.values()), sorted(removed), str(len(f.log)))


def _call(f: Feed, source: SourceView) -> None:
    """Make the feed's calls, as a provider's API calls, through the allowlist."""
    if not f.calls:
        return
    if source.credential is not None:
        f.tokens.append(source.credential)
    headers = {"Authorization": f"Bearer {source.credential}"} if source.credential else {}
    with outbound.client(TEST, transport=_SERVED, headers=headers) as http:
        for url in f.calls:
            http.get(url)


class TestProvider:
    """The adapter (`aios.source_providers.Adapter`) over a test feed, or (a connector's source)
    the feeds in its scope."""

    __test__ = False  # not a test class, whatever pytest makes of its name

    def _check(self, f: Feed, source: SourceView, *, read: bool = True) -> None:
        """Refuse as the feed's provider would; then, reading its changes or listing, make its
        calls."""
        if f.revoked:
            raise NeedsReconnecting("The test feed's access was revoked.")
        if f.broken:
            raise FeedBroken("The test feed is down.")
        if read:
            _call(f, source)

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        feeds = _scoped(source)
        if feeds is None:
            f = _feed_of(source)
            self._check(f, source)
            return _changes_in(f, int(cursor or 0))
        at = json.loads(cursor) if cursor else {}
        changed: list[Item] = []
        removed: list[str] = []
        for f in feeds:
            self._check(f, source)
            some = _changes_in(f, int(at.get(f.name, 0)))
            changed += [_prefixed(f, i) for i in some.changed]
            removed += [f"{f.name}/{r}" for r in some.removed]
            at[f.name] = len(f.log)
        return Changes(changed, removed, json.dumps(at, sort_keys=True))

    def listing(self, source: SourceView) -> Changes:
        feeds = _scoped(source)
        if feeds is None:
            f = _feed_of(source)
            self._check(f, source)
            return Changes([e.item for e in f.items.values()], [], str(len(f.log)))
        listed: list[Item] = []
        for f in feeds:
            self._check(f, source)
            listed += [_prefixed(f, e.item) for e in f.items.values()]
        return Changes(listed, [], json.dumps({f.name: len(f.log) for f in feeds}, sort_keys=True))

    def fetch(self, source: SourceView, item: Item) -> Content:
        if _scoped(source) is None:
            f, external_id = _feed_of(source), item.external_id
        else:
            name, external_id = item.external_id.split("/", 1)
            f = feed(name)
        self._check(f, source, read=False)
        if f.fail_after is not None:
            if f.fail_after <= 0:
                raise FeedBroken(f"The test feed failed fetching {item.external_id}.")
            f.fail_after -= 1
        f.fetched.append(external_id)
        return f.items[external_id].content

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        _pull()
        return [ScopeOption(name, name, FEEDS[name].restricted) for name in sorted(FEEDS)]


# --- As a connector -----------------------------------------------------------------------------

# The code the test provider's authorisation "page" sends back.
CODE = "test-code"


def _authorise_url(state: str) -> str:
    return "/api/connectors/callback?" + urlencode({"code": CODE, "state": state})


def _exchange(code: str) -> Grant:
    if code != CODE:
        raise ValueError("The test provider didn't recognise that code.")
    return Grant(secret=f"test-token-{secrets.token_urlsafe(24)}", scopes="read")


def _revoke(secret: str) -> None:
    REVOKED.append(secret)


def _keep_groups(rows: Iterable[SyncedItem]) -> list[KeptDocument]:
    """One Markdown document per feed (channel) per month (`kept.by_channel_and_month`)."""
    return kept.by_channel_and_month(
        rows, lambda r: r.external_id.split("/", 1)[0] if "/" in r.external_id else "test"
    )


# --- Its tools ----------------------------------------------------------------------------------

MCP = FakeServer()
CHANNEL = {"type": "string", "description": "The channel (feed): its name."}


def _post(args: dict) -> str:
    channel = load(args.get("channel", ""))
    if channel is None:
        raise ValueError(f"There's no channel {args.get('channel')!r}.")
    item = channel.message(f"posted-{len(channel.log)}", str(args.get("text", "")))
    save(channel.name)
    return f"Posted {item.external_id} to {channel.name}."


def _read(args: dict) -> str:
    channel = load(args.get("channel", ""))
    if channel is None:
        raise ValueError(f"There's no channel {args.get('channel')!r}.")
    return "\n".join(e.content.text or "" for e in channel.items.values())


def reset_tools() -> None:
    """The test provider's tools as they start: `post_message` and `read_channel`."""
    MCP.tools.clear()
    MCP.listings, MCP.calls[:], MCP.tokens[:] = 0, [], []
    MCP.add(
        FakeTool(
            "post_message",
            "Post a message to a channel.",
            {
                "type": "object",
                "properties": {"channel": CHANNEL, "text": {"type": "string"}},
                "required": ["channel", "text"],
            },
            reply=_post,
        )
    )
    MCP.add(
        FakeTool(
            "read_channel",
            "Read a channel's messages.",
            {"type": "object", "properties": {"channel": CHANNEL}, "required": ["channel"]},
            reply=_read,
            read_only=True,
        )
    )


reset_tools()


PROVIDER = Provider(
    TEST,
    "Test",
    _test_unavailable,
    adapter=TestProvider,
    authorise_url=_authorise_url,
    exchange=_exchange,
    revoke=_revoke,
    keep_groups=_keep_groups,
    tools=Tools("https://test.invalid/mcp", transport=MCP.transport),
)
