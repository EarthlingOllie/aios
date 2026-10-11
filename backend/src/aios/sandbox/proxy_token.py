"""Credentials for the egress proxy that carry a session's allowlist.

The API mints a token when it starts a sandbox, and the sandbox uses it as its proxy password.
The proxy only checks the signature and expiry, so it needs no per-session state. Code that
reads its own token gets nothing it didn't already have: the token only grants its own
session's domains, and can't be changed without the secret.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyGrant:
    session: str
    domains: tuple[str, ...]
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def mint(
    secret: str, *, session: str, domains: list[str], ttl: int, now: float | None = None
) -> str:
    expires_at = int((time.time() if now is None else now) + ttl)
    body = json.dumps({"s": session, "d": domains, "e": expires_at}, separators=(",", ":"))
    payload = _b64(body.encode())
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str, now: float | None = None) -> ProxyGrant | None:
    """The token's grant, or None if it's malformed, forged or expired."""
    payload, _, signature = token.partition(".")
    if not secret or not hmac.compare_digest(signature, _sign(secret, payload)):
        return None
    try:
        body = json.loads(_unb64(payload))
        grant = ProxyGrant(str(body["s"]), tuple(map(str, body["d"])), int(body["e"]))
    except (ValueError, KeyError, TypeError):
        return None
    if grant.expires_at < (time.time() if now is None else now):
        return None
    return grant


def allows(domains: tuple[str, ...] | list[str], host: str) -> bool:
    """Whether `host` is listed: `example.com` exactly, or `*.example.com` for its subdomains."""
    host = host.lower().rstrip(".")
    for entry in domains:
        # `*.example.com` matches `a.example.com` but not `example.com` or `badexample.com`.
        if host == entry or (entry.startswith("*.") and host.endswith(entry[1:])):
            return True
    return False
