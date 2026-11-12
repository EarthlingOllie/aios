"""The UI stream adapter against Anthropic's real stream shapes.

`ChatAnthropic` runs against a mocked HTTP transport that replays Anthropic server-sent
events: thinking, text, server-side web search, and a tool call the agent runs.
"""

import json

import anthropic
import httpx2 as httpx
from conftest import take_turn
from langchain_anthropic import ChatAnthropic
from test_agents import agent_body, make_agent

from aios import llm
from aios.tools import hybrid_search


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_start(id: str) -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": id,
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    }


def block(index: int, content_block: dict, *deltas: dict) -> list[dict]:
    return [
        {"type": "content_block_start", "index": index, "content_block": content_block},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def message_end(stop_reason: str) -> list[dict]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 20},
        },
        {"type": "message_stop"},
    ]


SEARCH_RESULT = [
    {
        "type": "web_search_result",
        "url": "https://zfin.org",
        "title": "ZFIN",
        "encrypted_content": "abc",
        "page_age": None,
    }
]

FIRST = sse(
    message_start("msg_1"),
    *block(
        0,
        {"type": "thinking", "thinking": "", "signature": ""},
        {"type": "thinking_delta", "thinking": "Search first."},
        {"type": "signature_delta", "signature": "sig"},
    ),
    *block(1, {"type": "text", "text": ""}, {"type": "text_delta", "text": "Searching. "}),
    *block(
        2,
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}},
        {"type": "input_json_delta", "partial_json": '{"query": '},
        {"type": "input_json_delta", "partial_json": '"zebrafish"}'},
    ),
    *block(
        3,
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": SEARCH_RESULT},
    ),
    *block(4, {"type": "text", "text": ""}, {"type": "text_delta", "text": "Now my notes."}),
    *block(
        5,
        {"type": "tool_use", "id": "toolu_1", "name": "search_documents", "input": {}},
        {"type": "input_json_delta", "partial_json": '{"query": "zebrafish"}'},
    ),
    *message_end("tool_use"),
)

SECOND = sse(
    message_start("msg_2"),
    *block(0, {"type": "text", "text": ""}, {"type": "text_delta", "text": "Zebrafish."}),
    *message_end("end_turn"),
)


def anthropic_model(requests: list[dict]) -> ChatAnthropic:
    replies = [FIRST, SECOND]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=replies[len(requests) - 1], headers={"content-type": "text/event-stream"}
        )

    model = ChatAnthropic(model="claude-sonnet-5-5", api_key="test", streaming=True)
    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    model.__dict__["_async_client"] = client
    return model


def test_anthropic_stream_round_trip(api, monkeypatch):
    async def results(**kwargs):
        return [{"source": "fish.md", "headings": [], "text": "My zebrafish notes."}]

    monkeypatch.setattr(hybrid_search, "document_search_results", results)
    tools = [{"key": "web_search", "config": {}}, {"key": "hybrid_search", "config": {}}]
    agent = make_agent(api, "Researcher")
    agent = api.put(f"/api/agents/{agent['id']}", json=agent_body(agent, tools=tools)).json()
    run = api.post(f"/api/agents/{agent['id']}/runs").json()
    url = f"/api/agents/{agent['id']}/runs/{run['id']}"

    requests = []
    model = anthropic_model(requests)
    monkeypatch.setattr(llm, "chat_model", lambda name=None: model)
    chunks = take_turn(api, agent["id"], run["id"], "zebrafish?").chunks
    assert not [c for c in chunks if c["type"] == "error"], chunks

    def summary(c: dict) -> str:
        detail = c.get("delta") or c.get("toolName") or ""
        return f"{c['type']} {detail}".strip()

    assert [summary(c) for c in chunks if c["type"] != "tool-input-delta"] == [
        "start",
        "start-step",
        "reasoning-start",
        "reasoning-delta Search first.",
        "reasoning-end",
        "text-start",
        "text-delta Searching.",
        "text-end",
        "tool-input-start web_search",
        "tool-input-available web_search",
        "tool-output-available",
        "text-start",
        "text-delta Now my notes.",
        "text-end",
        "tool-input-start search_documents",
        "tool-input-available search_documents",
        "finish-step",
        "tool-output-available",
        "start-step",
        "text-start",
        "text-delta Zebrafish.",
        "text-end",
        "finish-step",
        "finish",
    ]
    by_type = {c["type"]: c for c in chunks if c.get("toolCallId") == "srvtoolu_1"}
    assert by_type["tool-input-available"]["input"] == {"query": "zebrafish"}
    assert by_type["tool-output-available"]["output"] == SEARCH_RESULT
    assert by_type["tool-output-available"]["providerExecuted"] is True

    # The web search tool is sent in its basic version beside one of our tools, and the second
    # request replays the first response's blocks (thinking, server tool use and its results)
    # unchanged.
    assert {"type": "web_search_20250305", "name": "web_search", "max_uses": 5} in requests[0][
        "tools"
    ]
    replayed = [b["type"] for b in requests[1]["messages"][1]["content"]]
    assert replayed == [
        "thinking",
        "text",
        "server_tool_use",
        "web_search_tool_result",
        "text",
        "tool_use",
    ]
    assert requests[1]["messages"][2]["content"][0]["type"] == "tool_result"

    # The stored run renders like the live stream did.
    detail = api.get(url).json()
    parts = detail["ui_messages"][1]["parts"]
    assert [p["type"] for p in parts] == [
        "step-start",
        "reasoning",
        "text",
        "tool-web_search",
        "text",
        "tool-search_documents",
        "step-start",
        "text",
    ]
    web, notes = parts[3], parts[5]
    assert (web["state"], web["output"], web["providerExecuted"]) == (
        "output-available",
        SEARCH_RESULT,
        True,
    )
    assert (notes["state"], notes["output"]) == (
        "output-available",
        [{"source": "fish.md", "headings": [], "text": "My zebrafish notes."}],
    )
    assert detail["messages"][1]["usage_metadata"]["output_tokens"] == 20
