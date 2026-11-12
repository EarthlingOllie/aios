"""Calls a provider makes (syncs, and later tools), through an HTTP client that reaches only the
provider's allowed hosts (`connector_allowed_hosts`), and nothing at all once the provider is
switched off. A call anywhere else is refused before it's sent (`HostNotAllowed`), redirects
included; a sync that's refused fails with that recorded on it. Network-level egress control for
workers is out of scope: this is the HTTP client's check.
"""

import httpx

from aios.config import get_settings
from aios.source_providers import ProviderUnavailable, provider, why_unavailable


class HostNotAllowed(Exception):
    """A provider's call to a host outside its allowed list."""


def allowed_hosts(name: str) -> set[str]:
    return {h.lower() for h in get_settings().connector_allowed_hosts.get(name, [])}


def check(name: str, url: httpx.URL | str) -> None:
    """Refuse a call by the provider to `url` unless it's on one of its allowed hosts, over
    HTTPS (or HTTP), and the provider is on."""
    why = why_unavailable(name)
    if why is not None:
        raise ProviderUnavailable(why)
    url = httpx.URL(str(url))
    host = (url.host or "").lower()
    if url.scheme not in ("https", "http") or host not in allowed_hosts(name):
        found = provider(name)
        title = found.title if found else name
        raise HostNotAllowed(
            f"Refused a call to {host or str(url)}: it isn't one of the {title} provider's "
            "allowed hosts."
        )


def client(name: str, **kwargs) -> httpx.Client:
    """An HTTP client for the provider's calls: each request (and each redirect it follows) is
    checked first."""

    def on_request(request: httpx.Request) -> None:
        check(name, request.url)

    kwargs.setdefault("timeout", 30)
    hooks = kwargs.pop("event_hooks", {})
    hooks = {**hooks, "request": [on_request, *hooks.get("request", [])]}
    return httpx.Client(event_hooks=hooks, **kwargs)
