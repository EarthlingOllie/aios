"""Scripted scenarios a running app can name as its model: `scripted:<scenario>`.

With scripted models enabled (`SCRIPTED_MODELS_ENABLED`, off by default), `LLM_MODEL` and
`HANDOFF_MODEL` may name a scenario here, and the app runs on the scripted chat model the backend
tests use (`aios.testing.fake_model`) instead of a provider's; embeddings are a deterministic fake
too (`aios.embeddings`). The full-stack browser test runs the stack this way, so it makes no
calls to model, embedding or tracing providers (Compose's `fullstack` profile).

Every scenario answers the handoff (`aios.handoff`) with "On it." and an empty brief, so the
deployment's model can be the handoff's too. Later steps add scenarios here, one per story the
full-stack test tells.
"""

from collections.abc import AsyncIterator, Callable

from langchain_core.messages import BaseMessage

from aios.testing.fake_model import (
    FakeChatModel,
    Info,
    Script,
    ToolCall,
    last_tool_results,
    model,
    tool_output,
)

PROVIDER = "scripted"

# The handoff's structured output (`aios.handoff.Handoff`), as the model is asked for it.
HANDOFF = "Handoff"
ACK = "On it."

# The core loop's table, and what the agent says once it has written its row.
NOTES = {
    "op": "create_table",
    "name": "notes",
    "description": "Notes taken in chats",
    "columns": [{"name": "body", "type": "text", "nullable": False}],
}
SAVED = "Saved."


def human_text(messages: list[BaseMessage]) -> str:
    """The last message the model was given from the person (or the run's input)."""
    for message in reversed(messages):
        if message.type == "human":
            return message.text
    return ""


async def core_loop(messages: list[BaseMessage], info: Info) -> AsyncIterator[str | ToolCall]:
    """Asks for a `notes` table; once it's applied, writes the message to it as a note and
    replies "Saved."; says why if the table was refused or the write failed."""
    results = last_tool_results(messages)
    if not results:
        yield ToolCall(
            "request_schema_change", {"ops": [NOTES], "reason": "To keep the chat's notes"}
        )
        return
    last = results[-1]
    outcome = tool_output(last)
    if last.name == "request_schema_change":
        if not isinstance(outcome, dict) or outcome.get("status") != "applied":
            yield f"No notes table: {outcome}"
            return
        yield ToolCall(
            "write_data",
            {"table": "notes", "operation": "insert", "rows": [{"body": human_text(messages)}]},
        )
    elif isinstance(outcome, dict) and "error" in outcome:
        yield f"Couldn't save it: {outcome['error']}"
    else:
        yield SAVED


SCENARIOS: dict[str, Script] = {
    "core-loop": core_loop,
}


def handing_off(script: Script) -> Script:
    """`script`, answering the handoff's request for a brief itself."""

    async def scripted(messages: list[BaseMessage], info: Info):
        if HANDOFF in info.schemas:
            yield ToolCall(HANDOFF, {"ack": ACK, "brief": ""})
            return
        async for item in script(messages, info):
            yield item

    return scripted


class UnknownScenario(ValueError):
    pass


def chat_model(scenario: str) -> FakeChatModel:
    """The scripted model playing `scenario`. Raises `UnknownScenario` for one there isn't."""
    script: Callable | None = SCENARIOS.get(scenario)
    if script is None:
        raise UnknownScenario(
            f"Unknown scripted scenario {scenario!r}; one of: {', '.join(sorted(SCENARIOS))}"
        )
    return model(handing_off(script))
