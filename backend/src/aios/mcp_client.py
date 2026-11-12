"""The MCP client: lists and calls an MCP server's tools over MCP's streamable HTTP transport
(the `mcp` SDK, pinned exactly), inside the run service's and sync processes.

A server (`Server`) is its endpoint, the bearer token it's called with (never printed), and the
check every request passes first (`aios.outbound.check` for a provider's: only its allowed hosts);
a transport is given only to reach a server inside the process (the fake one, and the test
provider's). Each listing or call is a session of its own. Anything that goes wrong (a refused or
failed request, the server's protocol error) is an `McpError`; a tool's own error is a result
(`Result.is_error`).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(30, read=120)


class McpError(Exception):
    """A listing or call that couldn't be made, or failed in the protocol."""


@dataclass(frozen=True)
class Server:
    url: str
    token: str | None = field(default=None, repr=False)
    # Hands requests to a server in the process (tests, the test provider); None for the network.
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    # Run on each request (and redirect) before it's sent: raises to refuse it.
    check: Callable[[httpx.URL], None] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Marked read-only by the server (`readOnlyHint`): calling it changes nothing. Tools not
    # marked are taken to have side effects.
    read_only: bool = False


@dataclass(frozen=True)
class Result:
    text: str
    is_error: bool = False


def _client(server: Server) -> httpx.AsyncClient:
    async def on_request(request: httpx.Request) -> None:
        if server.check is not None:
            server.check(request.url)

    headers = {"Authorization": f"Bearer {server.token}"} if server.token else {}
    return httpx.AsyncClient(
        headers=headers,
        timeout=TIMEOUT,
        transport=server.transport,
        event_hooks={"request": [on_request]},
    )


async def _session(server: Server, use: Callable[[ClientSession], Any]) -> Any:
    try:
        async with (
            _client(server) as http,
            streamable_http_client(server.url, http_client=http, terminate_on_close=False) as (
                read,
                write,
                _,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await use(session)
    except McpError:
        raise
    except BaseException as exc:
        # The SDK's task groups wrap what failed: the first leaf says why.
        cause = _leaf(exc)
        if not isinstance(cause, Exception):
            raise
        log.warning(
            "MCP request to %s failed: %s", httpx.URL(server.url).host, type(cause).__name__
        )
        raise McpError(str(cause) or type(cause).__name__) from cause


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def list_tools(server: Server) -> list[Tool]:
    """The server's tools, every page of them."""

    async def use(session: ClientSession) -> list[Tool]:
        tools: list[Tool] = []
        cursor = None
        while True:
            page = await session.list_tools(
                params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            for t in page.tools:
                hints = t.annotations
                tools.append(
                    Tool(
                        name=t.name,
                        description=t.description or "",
                        input_schema=dict(t.inputSchema or {}),
                        read_only=bool(hints and hints.readOnlyHint),
                    )
                )
            cursor = page.nextCursor
            if not cursor:
                return tools

    return await _session(server, use)


def _text(content: list[types.ContentBlock]) -> str:
    parts = []
    for block in content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content]")
    return "\n".join(parts)


async def call_tool(server: Server, name: str, arguments: dict[str, Any]) -> Result:
    """Call one of the server's tools: its result as text, and whether it's the tool's error."""

    async def use(session: ClientSession) -> Result:
        result = await session.call_tool(name, arguments)
        return Result(_text(result.content), bool(result.isError))

    return await _session(server, use)
