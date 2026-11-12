"""Scripted scenarios a running app can name as its model: `scripted:<scenario>`.

With scripted models enabled (`SCRIPTED_MODELS_ENABLED`, off by default), `LLM_MODEL` and
`HANDOFF_MODEL` may name a scenario here, and the app runs on the scripted chat model the backend
tests use (`aios.testing.fake_model`) instead of a provider's; embeddings are a deterministic fake
too (`aios.embeddings`). The full-stack browser test runs the stack this way, so it makes no
calls to model, embedding or tracing providers (Compose's `fullstack` profile).

Every scenario answers the handoff (`aios.handoff`) with "On it." and an empty brief, and the
handback (`aios.handback`) with the output it was sent, so the deployment's model can be theirs
too. Later steps add scenarios here, one per story the
full-stack test tells.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

from langchain_core.messages import BaseMessage

from aios import jev
from aios.testing.fake_model import (
    FakeChatModel,
    Info,
    Script,
    ToolCall,
    handback_output,
    instructions,
    last_tool_results,
    model,
    tool_output,
)

PROVIDER = "scripted"

# The handoff's structured output (`aios.handoff.Handoff`), as the model is asked for it.
HANDOFF = "Handoff"
ACK = "On it."
# The handback's (`aios.handback.Handback`): its summary is the output, as it came.
HANDBACK = "Handback"

# The core loop's table, and what the agent says once it has written its row.
NOTES = {
    "op": "create_table",
    "name": "notes",
    "description": "Notes taken in chats",
    "columns": [{"name": "body", "type": "text", "nullable": False}],
}
SAVED = "Saved."
# A message asking the agent to take its time (`SLOWLY` in it) is answered after `SLOW_SECONDS`,
# longer than the full-stack run service's shutdown grace: so restarting the run service then
# finds the turn mid-call, and hands it back.
SLOWLY = "slowly"
SLOW_SECONDS = 6.0
WRITTEN_SLOWLY = "Written, slowly."
# The approval story's agents, by their instructions: one classifies a spend, giving structured
# output (`aios.workflows.structured.OUTPUT_TOOL`); the others reply as their instructions say.
CLASSIFY = "Classify the spend."
SPEND = {"risk": "high", "amount": 250}
REPLY_WITH = "Reply with: "


def human_text(messages: list[BaseMessage]) -> str:
    """The last message the model was given from the person (or the run's input), out of the
    untrusted block a chat message comes in (`aios.invocations.run_input`)."""
    for message in reversed(messages):
        if message.type == "human":
            return jev.unwrap(message.text)
    return ""


async def core_loop(messages: list[BaseMessage], info: Info) -> AsyncIterator[str | ToolCall]:
    """Asks for a `notes` table; once it's applied, writes the message to it as a note and
    replies "Saved."; says why if the table was refused or the write failed. Asked to take its
    time, it does, and replies "Written, slowly." (asking for nothing).

    In a workflow, an agent told to classify a spend gives `SPEND` as its structured output, and
    one told "Reply with: <text>" replies <text>."""
    from aios.workflows.structured import OUTPUT_TOOL

    told = instructions(messages)
    if told == CLASSIFY:
        yield ToolCall(OUTPUT_TOOL, SPEND)
        return
    if told.startswith(REPLY_WITH):
        yield told.removeprefix(REPLY_WITH)
        return
    results = last_tool_results(messages)
    if SLOWLY in human_text(messages):
        await asyncio.sleep(SLOW_SECONDS)
        yield WRITTEN_SLOWLY
        return
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
    """`script`, answering the handoff's request for a brief, and the handback's for a summary,
    itself."""

    async def scripted(messages: list[BaseMessage], info: Info):
        if HANDOFF in info.schemas:
            yield ToolCall(HANDOFF, {"ack": ACK, "brief": ""})
            return
        if HANDBACK in info.schemas:
            yield ToolCall(HANDBACK, {"summary": handback_output(messages)})
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
