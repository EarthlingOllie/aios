"""What each kind of source plugs into the shared sync loop (`aios.sync`) through: one adapter
interface (`Adapter`), which can

- list the changes since a cursor (`changes`);
- list everything in scope, for reconciliation (`listing`);
- fetch an item's content (`fetch`);
- describe its scope options (`scope_options`).

An item (`Item`) is named by its external id, and carries a content hash that changes whenever
its content does, so the loop fetches and indexes again only what changed. A provider raises
`NeedsReconnecting` on a final authorisation failure, and anything else when a sync can't go on.

An adapter can leave a file out (unsupported, or too big): it lists it as skipped (`Skipped`, in
`Changes.skipped`), or, finding out only as it fetches it, raises `SkipItem`. The sync records it on
its sync record and removes any copy it had. A change feed that can't say exactly what changed
(a folder moved) can answer a full listing instead (`Changes.complete`).

**Providers** (`PROVIDERS`) are found by name: Slack, Google Drive
(`aios.source_providers.google_drive`), GitHub (`aios.source_providers.github`) and the
development-only test provider
(`aios.source_providers.test`). Each says why it's unavailable here,
if it is (`why_unavailable`): switched off in this deployment (`connector_enabled_providers`, which
stops its syncs at once and leaves its content), missing its app settings, or not built yet. A
connector's provider also runs its connect flow (`Provider.authorise_url`, `exchange`, `revoke`)
and says how its message items are kept as documents when its connector is removed
(`Provider.keep_groups`). Its calls go through `aios.outbound`, which reaches only its hosts.

A provider with tools says where they're served (`Provider.tools`): its MCP server, which the
connector tool kind lists and calls (`aios.tools.connector`, `aios.mcp_client`), or aios itself
(GitHub's, called natively).
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


class SkipItem(Exception):
    """An item can't be synced (unsupported, or too big), found out as it's fetched: the sync
    leaves it out, listing it as skipped with this reason."""


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
class Skipped:
    """An item at its source that isn't synced (an unsupported or oversized file), and why."""

    external_id: str
    title: str
    reason: str


@dataclass(frozen=True)
class Changes:
    """What changed since a cursor: items added or changed, external ids removed, and the
    cursor to resume from once these are written; items left out (`skipped`), whose copies
    are removed; and whether it's everything in scope after all (`complete`: what it lacks is
    removed, as a full listing's is)."""

    changed: list[Item] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cursor: str | None = None
    skipped: list[Skipped] = field(default_factory=list)
    complete: bool = False


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
    connector's credential, through `aios.outbound`'s check), or aios itself (`listing` and
    `invoke`, for a provider with no MCP server aios can use); and the arguments that name what a
    call acts on (a channel, a repository), for the audit log and the allowed lists."""

    url: str | None = None
    # Hands requests to a server inside the process (the test provider's); None for the network.
    transport: Callable[[], httpx.AsyncBaseTransport] | None = None
    target_args: tuple[str, ...] = ("channel", "repository", "repo", "folder")
    # Served by aios: the tools (`aios.mcp_client.Tool`s), and a call (the connector's
    # credential, the tool's name, its arguments and the calling agent's name) that answers its
    # result's text and whether it's an error.
    listing: Callable[[], list[Any]] | None = None
    invoke: Callable[[str, str, dict[str, Any], str], tuple[str, bool]] | None = None
    # Every call that acts on a target must name one on the agent's allowed list for the
    # connector (`aios.tools.connector.ConnectorPin.targets`), as what it acts on is called here
    # ("repositories").
    targets_required: bool = False
    targets_name: str = "targets"


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
    # Takes the code, and anything else the provider sent back that it names (GitHub's
    # `installation_id`).
    exchange: Callable[..., Grant] | None = None
    # Revokes a grant at the provider, where it can.
    revoke: Callable[[str], None] | None = None
    # Its message items as documents, grouped as the provider's content is (a removed connector's
    # content, kept).
    keep_groups: Callable[[Iterable["SyncedItem"]], list[KeptDocument]] | None = None
    # Its tools, for agents; None if it has none (yet).
    tools: Tools | None = None
    # It can also sync documentation files (a source's `docs` setting): GitHub's README and docs
    # Markdown.
    docs: bool = False


def needs_app_settings(title: str, *values: Any) -> str | None:
    """Why a provider is unavailable for want of its app settings (None if it has them all)."""

    def present(v: Any) -> bool:
        return bool(v.get_secret_value() if hasattr(v, "get_secret_value") else v)

    if all(present(v) for v in values):
        return None
    return f"{title} isn't set up in this deployment: it needs its app settings."


def _app_settings(title: str, *values: Any) -> str | None:
    """A provider not built yet: unavailable whatever its settings."""
    return needs_app_settings(title, *values) or (
        f"{title} isn't available in this version of aios yet."
    )


def _test_unavailable() -> str | None:
    if get_settings().sources_test_provider_enabled:
        return None
    return "The test provider is for development only."


def _test_provider() -> Provider:
    from aios.source_providers import test

    return test.PROVIDER


def _google_drive_provider() -> Provider:
    from aios.source_providers import google_drive

    return google_drive.PROVIDER


def _github_provider() -> Provider:
    from aios.source_providers import github

    return github.PROVIDER


TEST = "test"


def _providers() -> dict[str, Provider]:
    s = get_settings()
    return {
        "slack": Provider(
            "slack",
            "Slack",
            lambda: _app_settings("Slack", s.slack_client_id, s.slack_client_secret),
        ),
        "google_drive": _google_drive_provider(),
        "github": _github_provider(),
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
