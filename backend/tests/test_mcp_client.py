"""The MCP client (`aios.mcp_client`): listing and calling a server's tools over MCP's streamable
HTTP transport, against the fake MCP connector server (`aios.testing.fake_mcp`), which serves
recorded responses inside the test process."""

import asyncio

import pytest

from aios import mcp_client
from aios.testing.fake_mcp import FakeServer, FakeTool

URL = "https://mcp.test.invalid/mcp"


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.add(
        FakeTool(
            "post_message",
            "Post a message to a channel.",
            {
                "type": "object",
                "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                "required": ["channel", "text"],
            },
            reply="Posted.",
        )
    )
    fake.add(FakeTool("list_channels", "List the channels.", reply="general", read_only=True))
    return fake


def connect(server: FakeServer, token: str = "tok") -> mcp_client.Server:
    return mcp_client.Server(URL, token=token, transport=server.transport())


def test_tools_are_listed_with_their_schemas_and_whether_theyre_read_only(server):
    tools = asyncio.run(mcp_client.list_tools(connect(server)))

    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"post_message", "list_channels"}
    assert by_name["post_message"].description == "Post a message to a channel."
    assert by_name["post_message"].input_schema["required"] == ["channel", "text"]
    assert (by_name["post_message"].read_only, by_name["list_channels"].read_only) == (False, True)
    assert server.listings == 1


def test_a_call_returns_the_recorded_response_and_the_server_sees_the_token(server):
    result = asyncio.run(
        mcp_client.call_tool(connect(server), "post_message", {"channel": "c", "text": "hi"})
    )

    assert (result.text, result.is_error) == ("Posted.", False)
    assert server.calls == [("post_message", {"channel": "c", "text": "hi"})]
    assert set(server.tokens) == {"tok"}


def test_a_tool_error_comes_back_as_an_error(server):
    server.add(FakeTool("fails", "Always fails.", error="The channel is archived."))

    result = asyncio.run(mcp_client.call_tool(connect(server), "fails", {}))

    assert result.is_error and "The channel is archived." in result.text


def test_a_request_its_check_refuses_isnt_sent(server):
    def refuse(url):
        raise ValueError(f"{url.host} isn't allowed")

    refused = mcp_client.Server(URL, token="tok", transport=server.transport(), check=refuse)
    with pytest.raises(mcp_client.McpError, match="mcp.test.invalid isn't allowed"):
        asyncio.run(mcp_client.list_tools(refused))
    assert server.listings == 0 and server.tokens == []
