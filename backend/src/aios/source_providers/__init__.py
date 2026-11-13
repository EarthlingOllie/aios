"""What each kind of source plugs into the shared sync loop (`aios.sync`) through: one adapter
interface (`Adapter`), which can

- list the changes since a cursor (`changes`);
- list everything in scope, for reconciliation (`listing`);
- fetch an item's content (`fetch`);
- describe its scope options (`scope_options`).

An item (`Item`) is named by its external id, and carries a content hash that changes whenever
its content does, so the loop fetches and indexes again only what changed. A provider raises
`NeedsReconnecting` on a final authorisation failure, and anything else when a sync can't go on.

**Providers** (`PROVIDERS`) are found by name: Slack (`aios.source_providers.slack`), Google
Drive, GitHub and the development-only test provider (`aios.source_providers.test`). Each
says why it's unavailable here, if it is (`why_unavailable`): switched off in this deployment
(`connector_enabled_providers`, which stops its syncs at once and leaves its content), missing
its app settings, or not built yet. A
connector's provider also runs its connect flow (`Provider.authorise_url`, `exchange`, `revoke`)
and says how its message items are kept as documents when its connector is removed
(`Provider.keep_groups`). Its calls go through `aios.outbound`, which reaches only its hosts.

A provider with tools says where they're served (`Provider.tools`): its MCP server, which the
connector tool kind lists and calls (`aios.tools.connector`, `aios.mcp_client`).
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from aios.config import get_settings
from aios.datastores.models import SyncedItemKind

if TYPE_CHECKING:
    from aios.datastores.models import SyncedItem


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
    # What this read went over, if not everything (a listing) or only what changed (a change
    # feed): the stored items (by external id) it covers. A stored item it covers that it didn't
    # return is gone at the source, and is removed. A provider that re-reads a window of its
    # history (Slack) says which items that window holds; a full listing that covers everything
    # needn't say.
    covers: Callable[[str], bool] | None = field(default=None, compare=False)
    # Parts of the scope that need an admin's attention (a channel archived, or left by the app),
    # by scope option id, each with why; None leaves what the source last recorded.
    attention: dict[str, str] | None = None


@dataclass(frozen=True)
class ScopeOption:
    id: str
    name: str
    # Restricted at the source (private): picking it needs an acknowledgement.
    restricted: bool = False


@dataclass(frozen=True)
class SourceView:
    """What an adapter is told of the source it syncs: with its connector's credential (never
    printed), for a connector's."""

    id: int
    name: str
    settings: dict[str, Any]
    credential: str | None = field(default=None, repr=False)
    # An admin asked for a full resync: re-read everything, however old.
    resync: bool = False
    # Where its last sync left off (what `Adapter.changes` is also given), for a listing that
    # re-reads only part of the source's history.
    cursor: str | None = None


class Adapter(Protocol):
    def changes(self, source: SourceView, cursor: str | None) -> Changes: ...

    def listing(self, source: SourceView) -> Changes: ...

    def fetch(self, source: SourceView, item: Item) -> Content: ...

    def scope_options(self, source: SourceView) -> list[ScopeOption]: ...


@dataclass(frozen=True)
class Grant:
    """What a provider granted at the end of a connect flow: the secret (never printed) and what
    isn't secret about it."""

    secret: str = field(repr=False)
    kind: str = "oauth"
    scopes: str | None = None
    expires_at: datetime | None = None
    external_account: str | None = None


@dataclass(frozen=True)
class KeptDocument:
    """A document made from a removed connector's message items, kept as an upload."""

    filename: str
    markdown: str


@dataclass(frozen=True)
class Tools:
    """Where a provider's tools are served: its MCP server's endpoint (called with the
    connector's credential, through `aios.outbound`'s check), and the arguments that name what a
    call acts on (a channel, a repository), for the audit log."""

    url: str
    # Hands requests to a server inside the process (the test provider's, and the tools aios
    # serves itself for a provider); None for the network.
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None
    target_args: tuple[str, ...] = ("channel", "repository", "repo", "folder")
    # Tools that may act only on targets an editor allowed (`ConnectorPin.allowed`), each with the
    # argument naming its target: a call on any other target is refused, and the model told.
    limited: dict[str, str] = field(default_factory=dict)
    # Tools whose text is signed with the agent's name ("— <agent name> via aios"), each with the
    # argument holding the text.
    signed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Provider:
    name: str
    title: str
    # Why it can't be used here whatever the enabled list says (missing app settings, not built
    # yet); None if it can.
    unavailable: Callable[[], str | None]
    # Its adapter; None until it's built.
    adapter: Callable[[], Adapter] | None = None
    # The connect flow: where to send the admin's browser with the signed state, and what the
    # code it comes back with is worth. None: it can't be connected (yet).
    authorise_url: Callable[[str], str] | None = None
    exchange: Callable[[str], Grant] | None = None
    # Revokes a grant at the provider, where it can.
    revoke: Callable[[str], None] | None = None
    # Its message items as documents, grouped as the provider's content is (a removed connector's
    # content, kept).
    keep_groups: Callable[[Iterable["SyncedItem"]], list[KeptDocument]] | None = None
    # Its tools, for agents; None if it has none (yet).
    tools: Tools | None = None
    # What an admin picking its scope should know (why something isn't listed, say).
    scope_hint: str | None = None


def _app_settings(title: str, *values: Any) -> str | None:
    def present(v: Any) -> bool:
        return bool(v.get_secret_value() if hasattr(v, "get_secret_value") else v)

    if all(present(v) for v in values):
        return f"{title} isn't available in this version of aios yet."
    return f"{title} isn't set up in this deployment: it needs its app settings."


def _test_unavailable() -> str | None:
    if get_settings().sources_test_provider_enabled:
        return None
    return "The test provider is for development only."


def _test_provider() -> Provider:
    from aios.source_providers import test

    return test.PROVIDER


TEST = "test"


def _slack_provider() -> Provider:
    from aios.source_providers import slack

    return slack.PROVIDER


def _providers() -> dict[str, Provider]:
    s = get_settings()
    return {
        "slack": _slack_provider(),
        "google_drive": Provider(
            "google_drive",
            "Google Drive",
            lambda: _app_settings("Google Drive", s.google_client_id, s.google_client_secret),
        ),
        "github": Provider(
            "github",
            "GitHub",
            lambda: _app_settings(
                "GitHub", s.github_app_id, s.github_app_private_key, s.github_app_slug
            ),
        ),
        TEST: _test_provider(),
    }


def provider(name: str | None) -> Provider | None:
    return _providers().get(name or "")


def all_providers() -> list[Provider]:
    return list(_providers().values())


def why_unavailable(name: str | None) -> str | None:
    """Why the provider can't be used here (None if it can): there's none by that name, it's
    switched off in this deployment, or it's unavailable of itself."""
    found = provider(name)
    if found is None:
        return f"There's no provider {name!r}."
    if found.name not in get_settings().connector_enabled_providers:
        return f"{found.title} is switched off in this deployment."
    return found.unavailable()


def available() -> dict[str, str | None]:
    """The providers a source can be added with directly (not through a connector) here, each
    with why it's unavailable (None if it's available): the test provider."""
    return {TEST: why_unavailable(TEST)}


def adapter_for(settings: dict[str, Any]) -> Adapter:
    """The adapter for a source with these settings. Raises `ProviderUnavailable`."""
    name = settings.get("provider")
    why = why_unavailable(name)
    found = provider(name)
    if why is None and (found is None or found.adapter is None):
        why = f"There's no adapter for {name!r}."
    if why is not None:
        raise ProviderUnavailable(why)
    return found.adapter()
