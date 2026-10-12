"""The egress proxy: a sandbox's only way out (`python -m aios.sandbox.egress_proxy`).

An HTTP CONNECT proxy for HTTPS (port 443) only. Each request must carry a proxy token (see
`aios.sandbox.proxy_token`) as its Basic-auth password, and may only reach the domains the token
lists. The proxy resolves the host itself and only connects to globally routable addresses, so
an allowed name that resolves into the VPC (RDS, Redis, the API, the metadata endpoint) is
refused, and DNS rebinding can't get around that. TLS passes through untouched.
"""

import argparse
import asyncio
import base64
import binascii
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable

from aios.sandbox.proxy_token import ProxyGrant, allows, verify

log = logging.getLogger("aios.egress_proxy")

HEAD_LIMIT = 8192
HEAD_TIMEOUT = 10
CONNECT_TIMEOUT = 10

Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


class Refused(Exception):
    def __init__(self, status: str, reason: str, headers: str = "") -> None:
        super().__init__(reason)
        self.status, self.reason, self.headers = status, reason, headers


def _grant(head: list[str], secret: str) -> ProxyGrant:
    for line in head[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() != "proxy-authorization":
            continue
        scheme, _, encoded = value.strip().partition(" ")
        if scheme.lower() == "basic":
            try:
                _, _, token = base64.b64decode(encoded).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                break
            if grant := verify(secret, token):
                return grant
        break
    raise Refused(
        "407 Proxy Authentication Required",
        "missing or invalid proxy token",
        'Proxy-Authenticate: Basic realm="sandbox"\r\n',
    )


def _target(request_line: str) -> tuple[str, int]:
    method, _, rest = request_line.partition(" ")
    target = rest.partition(" ")[0]
    if method != "CONNECT":
        raise Refused("405 Method Not Allowed", "only HTTPS (CONNECT) is supported")
    host, _, port = target.rpartition(":")
    if not host or port != "443":
        raise Refused("403 Forbidden", "only port 443 is allowed")
    return host.strip("[]").lower().rstrip("."), 443


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    secret: str,
    resolver: Resolver = resolve,
    connect: Connector = asyncio.open_connection,
    allow_private: bool = False,
) -> None:
    session, host = "-", "-"
    try:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            raise Refused("400 Bad Request", "malformed request") from None
        head = raw.decode("latin-1").split("\r\n")
        host, port = _target(head[0])
        grant = _grant(head, secret)
        session = grant.session
        if not allows(grant.domains, host):
            raise Refused("403 Forbidden", f"{host} isn't in this sandbox's allowed domains")
        try:
            addresses = await resolver(host, port)
        except OSError:
            raise Refused("502 Bad Gateway", f"can't resolve {host}") from None
        usable = [a for a in addresses if allow_private or ipaddress.ip_address(a).is_global]
        if not usable:
            raise Refused("403 Forbidden", f"{host} resolves to a private address")
        try:
            up_reader, up_writer = await asyncio.wait_for(connect(usable[0], port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError):
            raise Refused("502 Bad Gateway", f"can't connect to {host}") from None
    except Refused as refused:
        log.info("refused session=%s host=%s: %s", session, host, refused.reason)
        body = refused.reason + "\n"
        writer.write(
            f"HTTP/1.1 {refused.status}\r\n{refused.headers}Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode()
        )
        await writer.drain()
        writer.close()
        return

    log.info("connect session=%s host=%s address=%s", session, host, usable[0])
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


async def serve(host: str, port: int, secret: str) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, secret=secret), host, port, limit=HEAD_LIMIT
    )
    log.info("listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()


def main() -> None:
    from aios.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    args = parser.parse_args()
    secret = get_settings().sandbox_proxy_secret.get_secret_value()
    if not secret:
        raise SystemExit("SANDBOX_PROXY_SECRET is not set")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.host, args.port, secret))


if __name__ == "__main__":
    main()
