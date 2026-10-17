import json

from fake_model import ToolCall, last_tool_results, model, replies, tool_output, use_model

from aios import agent as agent_module
from aios.auth import current_user
from aios.datastores import data_store_for
from aios.main import app
from aios.models import User


def events(resp) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: {")
    ]


def test_chat_streams_ai_sdk_protocol(client, user):
    app.dependency_overrides[current_user] = lambda: user
    body = {
        "trigger": "submit-message",
        "id": "chat-1",
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    }
    try:
        with use_model(replies("hi there")):
            resp = client.post("/api/chat", json=body)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-vercel-ai-ui-message-stream"] == "v1"
    chunks = events(resp)
    assert "".join(e["delta"] for e in chunks if e["type"] == "text-delta") == "hi there"
    assert [e["type"] for e in chunks if "delta" not in e] == [
        "start",
        "start-step",
        "text-start",
        "text-end",
        "finish-step",
        "finish",
    ]
    assert resp.text.endswith("data: [DONE]\n\n")


def test_chat_searches_the_users_documents(client, user, monkeypatch):
    calls = []

    async def results(**kwargs):
        calls.append(kwargs)
        return [{"source": "fish.md", "headings": [], "text": "Zebrafish."}]

    async def script(messages, info):
        if found := last_tool_results(messages):
            yield f"found {tool_output(found[0])[0]['text']}"
        else:
            yield ToolCall("search_documents", {"query": "a"}, id="call-1")

    monkeypatch.setattr(agent_module, "document_search_results", results)
    app.dependency_overrides[current_user] = lambda: user
    body = {
        "trigger": "submit-message",
        "id": "chat-1",
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "fish?"}]}],
    }
    try:
        with use_model(model(script)):
            resp = client.post("/api/chat", json=body)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert calls == [{"data_store_id": data_store_for(user).id, "query": "a"}]
    chunks = events(resp)
    tool = [e for e in chunks if e["type"].startswith("tool-")]
    assert [e["type"] for e in tool] == [
        "tool-input-start",
        "tool-input-delta",
        "tool-input-available",
        "tool-output-available",
    ]
    assert tool[2]["input"] == {"query": "a"}
    assert tool[3]["output"] == [{"source": "fish.md", "headings": [], "text": "Zebrafish."}]
    assert "".join(e["delta"] for e in chunks if e["type"] == "text-delta") == "found Zebrafish."


def test_chat_replays_the_clients_conversation(client, user):
    """The client sends the whole conversation, tool calls and results included."""
    seen = []

    async def script(messages, info):
        seen.extend(messages)
        yield "ok"

    body = {
        "trigger": "submit-message",
        "id": "chat-1",
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "fish?"}]},
            {
                "id": "m2",
                "role": "assistant",
                "parts": [
                    {"type": "step-start"},
                    {
                        "type": "tool-search_documents",
                        "toolCallId": "c1",
                        "state": "output-available",
                        "input": {"query": "fish"},
                        "output": [{"text": "Zebrafish."}],
                    },
                    {"type": "step-start"},
                    {"type": "text", "text": "Zebrafish."},
                ],
            },
            {"id": "m3", "role": "user", "parts": [{"type": "text", "text": "more?"}]},
        ],
    }
    app.dependency_overrides[current_user] = lambda: user
    try:
        with use_model(model(script)):
            assert client.post("/api/chat", json=body).status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert [m.type for m in seen] == ["system", "human", "ai", "tool", "ai", "human"]
    assert seen[2].tool_calls[0]["args"] == {"query": "fish"}
    assert json.loads(seen[3].content) == [{"text": "Zebrafish."}]


def test_chat_rejects_a_malformed_body(client):
    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")
    try:
        assert client.post("/api/chat", json={"messages": []}).status_code == 422
        blank = {"messages": [{"id": "m", "role": "user", "parts": [{"type": "text", "text": ""}]}]}
        assert client.post("/api/chat", json=blank).status_code == 422
    finally:
        app.dependency_overrides.clear()
