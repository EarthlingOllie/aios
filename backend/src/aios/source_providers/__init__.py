"""What each kind of source plugs into the shared sync loop (`aios.sync`) through: one adapter
interface (`Adapter`), which can

- list the changes since a cursor (`changes`);
- list everything in scope, for reconciliation (`listing`);
- fetch an item's content (`fetch`);
- describe its scope options (`scope_options`).

An item (`Item`) is named by its external id, and carries a content hash that changes whenever
its content does, so the loop fetches and indexes again only what changed. A provider raises
`NeedsReconnecting` on a final authorisation failure, and anything else when a sync can't go on.

The providers are found by name (`adapter_for`): so far only the development-only test provider
(`aios.source_providers.test`); URL, webhook, email and connector sources add theirs.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from aios.config import get_settings
from aios.datastores.models import SyncedItemKind


class NeedsReconnecting(Exception):
    """The source's access is gone for good (a final 401): an admin must reconnect it."""


class ProviderUnavailable(Exception):
    """The source's provider isn't offered in this deployment."""


@dataclass(frozen=True)
class Item:
    """An item at its source, as a listing or change feed gives it."""

    external_id: str
    kind: SyncedItemKind
    # Its title, or (a file's) file name.
    title: str
    # Changes whenever its content does (its source's version, or a hash of the content).
    content_hash: str
    url: str | None = None
    author_name: str | None = None
    updated_at: datetime | None = None
    parent_external_id: str | None = None
    # A file's.
    content_type: str | None = None


@dataclass(frozen=True)
class Content:
    """An item's content: a message's text, or a file's bytes."""

    text: str | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class Changes:
    """What changed since a cursor: items added or changed, external ids removed, and the
    cursor to resume from once these are written."""

    changed: list[Item] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cursor: str | None = None


@dataclass(frozen=True)
class ScopeOption:
    id: str
    name: str
    # Restricted at the source (private): picking it needs an acknowledgement.
    restricted: bool = False


@dataclass(frozen=True)
class SourceView:
    """What an adapter is told of the source it syncs."""

    id: int
    name: str
    settings: dict[str, Any]


class Adapter(Protocol):
    def changes(self, source: SourceView, cursor: str | None) -> Changes: ...

    def listing(self, source: SourceView) -> Changes: ...

    def fetch(self, source: SourceView, item: Item) -> Content: ...

    def scope_options(self, source: SourceView) -> list[ScopeOption]: ...


TEST = "test"


def available() -> dict[str, str | None]:
    """The providers a source can be added with here, each with why it's unavailable (None if
    it's available)."""
    test_off = (
        None
        if get_settings().sources_test_provider_enabled
        else "The test provider is for development only."
    )
    return {TEST: test_off}


def adapter_for(settings: dict[str, Any]) -> Adapter:
    """The adapter for a source with these settings. Raises `ProviderUnavailable`."""
    provider = settings.get("provider")
    why = available().get(provider, f"There's no provider {provider!r}.")
    if why is not None:
        raise ProviderUnavailable(why)
    from aios.source_providers import test

    return test.TestProvider()
