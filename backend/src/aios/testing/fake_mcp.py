"""A fake MCP connector server with recorded responses: the test seam for connector tools
(`aios.tools.connector`) and the MCP client (`aios.mcp_client`), and what the test provider's
tools are served by (`aios.source_providers.test`).

It speaks MCP's streamable HTTP transport for real (the SDK's server, stateless, answering JSON),
but inside the process: `transport` is an httpx transport that hands each request to it, so
nothing leaves the machine. Its tools (`FakeTool`) are recorded: each has its listing (name,
description, input schema, whether it's read-only) and its reply (text, a function of the
arguments, or an error). A test adds and removes tools as the provider would, and sees what was
asked of it: the listings served (`listings`), the calls made (`calls`) and the bearer tokens
they came with (`tokens`).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass
class FakeTool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(EMPTY_SCHEMA))
    # What a call answers: text, or text made from the call's arguments.
    reply: str | Callable[[dict[str, Any]], str] = ""
    # A call fails with this, as the tool's error, instead.
    error: str | None = None
    # Marked read-only (MCP's `readOnlyHint`): calling it changes nothing.
    read_only: bool = False


class FakeServer:
    """One fake MCP server: its tools, and what it was asked."""

    def __init__(self) -> None:
        self.tools: dict[str, FakeTool] = {}
        self.listings = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tokens: list[str] = []
        self._server = self._build()

    def add(self, tool: FakeTool) -> FakeTool:
        self.tools[tool.name] = tool
        return tool

    def remove(self, name: str) -> None:
        """The tool vanishes upstream."""
        self.tools.pop(name, None)

    def _build(self) -> Server:
        server: Server = Server("aios-fake-mcp")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=t.name,
                    description=t.description,
                    inputSchema=t.input_schema,
                    annotations=types.ToolAnnotations(readOnlyHint=t.read_only),
                )
                for t in self.tools.values()
            ]

        # Validated here rather than by the SDK, which would list the tools to do it.
        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            self.calls.append((name, arguments))
            tool = self.tools.get(name)
            if tool is None:
                raise ValueError(f"Unknown tool: {name}")
            if tool.error is not None:
                raise ValueError(tool.error)
            text = tool.reply(arguments) if callable(tool.reply) else tool.reply
            return [types.TextContent(type="text", text=text)]

        return server

    async def _asgi(self, scope, receive, send) -> None:
        """The server as an ASGI app: notes the request's token and method, then answers it with
        a session manager of its own (stateless: each request stands alone)."""
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if auth.lower().startswith("bearer "):
            self.tokens.append(auth[7:])
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        self._note(body)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                await anyio.sleep_forever()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        manager = StreamableHTTPSessionManager(self._server, json_response=True, stateless=True)
        async with manager.run():
            await manager.handle_request(scope, replay, send)

    def _note(self, body: bytes) -> None:
        try:
            message = json.loads(body or b"null")
        except ValueError:
            return
        for one in message if isinstance(message, list) else [message]:
            if isinstance(one, dict) and one.get("method") == "tools/list":
                self.listings += 1

    def transport(self) -> httpx.AsyncBaseTransport:
        """An httpx transport that hands each request to this server, in the process."""
        return httpx.ASGITransport(app=self._asgi)
