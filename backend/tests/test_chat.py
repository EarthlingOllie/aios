import json

from pydantic_ai.models.test import TestModel

from aios.agent import agent
from aios.auth import current_user
from aios.main import app
from aios.models import User


def test_chat_streams_ai_sdk_protocol(client):
    app.dependency_overrides[current_user] = lambda: User(id=1, github_id=1, login="t")
    body = {
        "trigger": "submit-message",
        "id": "chat-1",
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    }
    try:
        with agent.override(model=TestModel(call_tools=[], custom_output_text="hi there")):
            resp = client.post("/api/chat", json=body)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: {")
    ]
    assert "".join(e["delta"] for e in events if e["type"] == "text-delta") == "hi there"
    assert "data: [DONE]" in resp.text
