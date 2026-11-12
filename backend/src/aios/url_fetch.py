"""Fetching a URL source's page (`aios.sync`), guarded against server-side request forgery:

- only HTTP or HTTPS, on ports 80 and 443;
- the host's addresses must all be public (not private, loopback, link-local, metadata, shared,
  reserved or multicast; an IPv4-mapped IPv6 address is judged as its IPv4 one), checked before
  every request, redirects included;
- the connection is made to the address that was checked (named by the host in its `Host`
  header and TLS server name), so a DNS answer that changes between the check and the connection
  is never used;
- at most `url_fetch_max_redirects` redirects, `url_fetch_max_bytes` of body and
  `url_fetch_timeout_seconds` in all.

A fetch is conditional when given the page's last ETag or Last-Modified; "not modified" and
"not found" or "gone" are answers (`NOT_MODIFIED`, `GONE`), not failures.

`resolve` and `transport` are the seams tests replace: names, and the connection.
"""

import ipaddress
import socket
import time
from dataclasses import dataclass

import httpx

from aios.config import get_settings

UNREACHABLE = "This address isn't reachable from aios"

OK = "ok"
NOT_MODIFIED = "not_modified"
GONE = "gone"

PORTS = (80, 443)
REDIRECTS = (301, 302, 303, 307, 308)
CHUNK = 64 * 1024


class Unreachable(ValueError):
    """The URL isn't one aios may reach: its scheme, port or address."""

    def __init__(self) -> None:
        super().__init__(UNREACHABLE)


class FetchFailed(Exception):
    """The page couldn't be fetched: an error answer, or past a limit."""


@dataclass(frozen=True)
class Target:
    url: httpx.URL
    host: str
    port: int
    # The checked address the connection goes to.
    address: str


@dataclass(frozen=True)
class Fetched:
    status: str
    # Where the page was found, after redirects.
    url: str
    body: bytes = b""
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None


def resolve(host: str) -> list[str]:
    """The host's addresses (the seam tests replace)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def transport() -> httpx.BaseTransport:
    """The connection (the seam tests replace)."""
    return httpx.HTTPTransport(retries=0)


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            ip = mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check(url: str | httpx.URL) -> Target:
    """The address the URL may be fetched from. Raises `Unreachable`."""
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, ValueError, TypeError):
        raise Unreachable() from None
    host = (parsed.host or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise Unreachable()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in PORTS:
        raise Unreachable()
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolve(host)
    if not addresses or not all(_public(a) for a in addresses):
        raise Unreachable()
    return Target(parsed, host, port, addresses[0])


def _request(target: Target, headers: dict[str, str]) -> httpx.Request:
    """A request to the checked address, naming the host."""
    address = f"[{target.address}]" if ":" in target.address else target.address
    url = target.url.copy_with(host=address, port=target.port)
    default = 443 if target.url.scheme == "https" else 80
    host = target.host if target.port == default else f"{target.host}:{target.port}"
    extensions = {}
    if target.url.scheme == "https":
        extensions["sni_hostname"] = target.host
    return httpx.Request("GET", url, headers={"Host": host, **headers}, extensions=extensions)


def fetch(url: str, *, etag: str | None = None, last_modified: str | None = None) -> Fetched:
    """Fetch the page (see the module). Raises `Unreachable` or `FetchFailed`."""
    settings = get_settings()
    deadline = time.monotonic() + settings.url_fetch_timeout_seconds
    headers = {"Accept": "text/html, text/plain, application/pdf, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    timeout = httpx.Timeout(settings.url_fetch_timeout_seconds)
    with httpx.Client(transport=transport(), timeout=timeout, trust_env=False) as client:
        current = httpx.URL(url)
        for _ in range(settings.url_fetch_max_redirects + 1):
            target = check(current)
            try:
                response = client.send(_request(target, headers), stream=True)
                try:
                    if response.status_code in REDIRECTS and "location" in response.headers:
                        current = target.url.join(response.headers["location"])
                        continue
                    return _answer(str(target.url), response, deadline)
                finally:
                    response.close()
            except httpx.TimeoutException:
                raise FetchFailed("The page took too long to fetch.") from None
            except httpx.HTTPError as exc:
                raise FetchFailed(f"The page couldn't be fetched: {exc}") from None
        raise FetchFailed(f"The page has more than {settings.url_fetch_max_redirects} redirects.")


def _answer(url: str, response: httpx.Response, deadline: float) -> Fetched:
    if response.status_code == 304:
        return Fetched(NOT_MODIFIED, url)
    if response.status_code in (404, 410):
        return Fetched(GONE, url)
    if response.status_code >= 400 or response.status_code < 200:
        raise FetchFailed(f"The page answered {response.status_code}.")
    limit = get_settings().url_fetch_max_bytes
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise FetchFailed(f"The page is too large (over {limit} bytes).")
    body = bytearray()
    for chunk in response.iter_bytes(CHUNK):
        body.extend(chunk)
        if len(body) > limit:
            raise FetchFailed(f"The page is too large (over {limit} bytes).")
        if time.monotonic() > deadline:
            raise FetchFailed("The page took too long to fetch.")
    if time.monotonic() > deadline:
        raise FetchFailed("The page took too long to fetch.")
    content_type = response.headers.get("content-type")
    return Fetched(
        OK,
        url,
        bytes(body),
        content_type.split(";")[0].strip().lower() if content_type else None,
        response.headers.get("etag"),
        response.headers.get("last-modified"),
    )
