"""Run the real API for a browser check of the data tools, with only the outside world faked.

- GitHub OAuth is faked so /auth/callback signs in a fixed user (github id -2600).
- The chat model is the tests' scripted fake (backend/tests/fake_model.py). It plays an agent
  that uses describe_data and query_data for real: the tools run against the user's real,
  provisioned store; only the model's choices are scripted.
  - "heaviest": describe_data, then a query built from what it described, then an answer
    built from the rows that came back.
  - "delete": query_data with a DELETE, then reports the tool's error.
  - "slow": query_data with pg_sleep past the timeout, then reports the error.
  - "other store <dbname>": a cross-database reference to another store.

Usage: python serve.py <port>
"""

import json
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path.cwd() / "tests"))

from fake_model import ToolCall, last_tool_results, model, tool_output  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from aios import auth, llm  # noqa: E402


def last_human(messages) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else json.dumps(m.content)
    return ""


async def script(messages, info):
    ask = last_human(messages).lower()
    results = [tool_output(m) for m in last_tool_results(messages)]
    print("MODEL sees tools:", sorted(info.tools), "results:", results, flush=True)
    if "heaviest" in ask:
        if not results:
            yield ToolCall("describe_data", {})
            return
        result = results[0]
        if "error" in result:
            yield f"The data tools refused: {result['error']}"
            return
        if "tables" in result:
            fish = next(t for t in result["tables"] if t["schema"] == "data")
            cols = [c["name"] for c in fish["columns"]]
            yield ToolCall(
                "query_data",
                {
                    "sql": f"SELECT {cols[1]}, {cols[2]} FROM data.{fish['name']}"
                    f" ORDER BY {cols[2]} DESC LIMIT 1"
                },
            )
            return
        name, weight = result["rows"][0]
        yield f"The heaviest catch is the **{name}**, at {weight} kg (from `data.fish`)."
        return
    if "delete" in ask:
        if not results:
            yield ToolCall("query_data", {"sql": "DELETE FROM data.fish RETURNING name"})
            return
        yield f"I couldn't delete anything: {results[0].get('error')}"
        return
    if "slow" in ask:
        if not results:
            yield ToolCall("query_data", {"sql": "SELECT pg_sleep(60)"})
            return
        yield f"The slow query was stopped: {results[0].get('error')}"
        return
    if "other store" in ask:
        dbname = ask.rsplit(" ", 1)[-1]
        if not results:
            yield ToolCall("query_data", {"sql": f"SELECT * FROM {dbname}.data.secrets"})
            return
        yield f"Reaching another store failed: {results[0].get('error')}"
        return
    yield "I only know how to answer a few questions."


FAKE = model(script)
llm.chat_model = lambda name=None: FAKE


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


async def _token(request):
    return {"access_token": "fake"}


async def _get(path, token):
    return _Resp({"id": -2600, "login": "ds26-check", "name": "DS26 Check", "email": "c@x"})


auth.oauth.github.authorize_access_token = _token
auth.oauth.github.get = _get

from aios.main import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="info")
