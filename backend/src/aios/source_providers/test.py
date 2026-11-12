"""The test provider: a source fed from an in-process feed (`Feed`), for development and tests
only (`sources_test_provider_enabled`). A source names its feed in its settings (`feed`); one not
registered with `feed` gets the sample feed's items.

A feed is a change log: putting or deleting an item adds a change, and a cursor is how many
changes have been read. A test can also delete an item without the change feed saying
(`delete(..., silently=True)`), so only a full listing catches it; make fetching fail after so
many items (`fail_after`), or for good (`broken`); or take its access away (`revoked`).
"""

import hashlib
from dataclasses import dataclass, field

from aios.datastores.models import SyncedItemKind
from aios.source_providers import (
    Changes,
    Content,
    Item,
    NeedsReconnecting,
    ScopeOption,
    SourceView,
)


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


def feed(name: str) -> Feed:
    """The feed called `name`, made empty if there's none."""
    return FEEDS.setdefault(name, Feed(name))


def forget(name: str) -> None:
    FEEDS.pop(name, None)


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
    name = source.settings.get("feed") or "sample"
    if name not in FEEDS and name == "sample":
        FEEDS[name] = _sample()
    return feed(name)


class TestProvider:
    """The adapter (`aios.source_providers.Adapter`) over a test feed."""

    __test__ = False  # not a test class, whatever pytest makes of its name

    def _check(self, f: Feed) -> None:
        if f.revoked:
            raise NeedsReconnecting("The test feed's access was revoked.")
        if f.broken:
            raise FeedBroken("The test feed is down.")

    def changes(self, source: SourceView, cursor: str | None) -> Changes:
        f = _feed_of(source)
        self._check(f)
        start = int(cursor or 0)
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

    def listing(self, source: SourceView) -> Changes:
        f = _feed_of(source)
        self._check(f)
        return Changes([e.item for e in f.items.values()], [], str(len(f.log)))

    def fetch(self, source: SourceView, item: Item) -> Content:
        f = _feed_of(source)
        self._check(f)
        if f.fail_after is not None:
            if f.fail_after <= 0:
                raise FeedBroken(f"The test feed failed fetching {item.external_id}.")
            f.fail_after -= 1
        f.fetched.append(item.external_id)
        return f.items[item.external_id].content

    def scope_options(self, source: SourceView) -> list[ScopeOption]:
        return [ScopeOption(name, name) for name in sorted(FEEDS)]
