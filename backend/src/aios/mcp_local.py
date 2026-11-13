"""MCP servers aios serves itself, in the process, for a provider whose tools it implements over
the provider's own API (Slack's `post_to_slack`): connector tools (`aios.tools.connector`) list
and call them over MCP's streamable HTTP transport like any provider's, through an httpx transport
that hands each request to the server (`LocalServer.transport`), so nothing is sent for the MCP
exchange itself. A tool's reply is given the call's arguments and the bearer token it came with
(the connector's credential), and makes its own calls to the provider through `aios.outbound`.
"""

import contextvars
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_local_token", default=None)


@dataclass
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    # Answers a call from its arguments and the bearer token: text, or raises with the error.
    reply: Callable[[dict[str, Any], str | None], str] = lambda arguments, token: ""
    read_only: bool = False


class LocalServer:
    def __init__(self, name: str, tools: list[LocalTool]) -> None:
        self.tools = {t.name: t for t in tools}
        self._server = self._build(name)

    def _build(self, name: str) -> Server:
        server: Server = Server(name)

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

        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            tool = self.tools.get(name)
            if tool is None:
                raise ValueError(f"Unknown tool: {name}")
            token = _token.get()
            text = await anyio.to_thread.run_sync(tool.reply, arguments or {}, token)
            return [types.TextContent(type="text", text=text)]

        return server

    async def _asgi(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        _token.set(auth[7:] if auth.lower().startswith("bearer ") else None)
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
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

    def transport(self) -> httpx.AsyncBaseTransport:
        """An httpx transport that hands each request to this server, in the process."""
        return httpx.ASGITransport(app=self._asgi)
