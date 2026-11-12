"""A scripted chat model, standing in for the provider's: the backend tests script it per test
(`use_model`, `use_handoff`), and a running app can name a scenario of it as its model
(`aios.testing.scenarios`, with scripted models enabled)."""

import html
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import agenerate_from_stream
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = field(default_factory=lambda: f"call_{uuid4().hex[:8]}")


@dataclass
class Usage:
    """What a response cost, as the provider reports it in the message's usage metadata."""

    input_tokens: int
    output_tokens: int


@dataclass
class Info:
    """What the model was given besides the messages."""

    # LangChain tools by name, and provider-run (server) tools as their definitions.
    tools: dict[str, BaseTool]
    server_tools: list[dict[str, Any]]
    # The schemas it was asked for a structured output in (e.g. the handoff's `Handoff`), by name.
    schemas: frozenset[str] = frozenset()


# Called with the messages the model is sent; yields text deltas, tool calls and usage.
Script = Callable[[list[BaseMessage], Info], AsyncIterator[str | ToolCall | Usage]]


class FakeChatModel(BaseChatModel):
    script: Any
    bound: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"bound": list(tools)})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError("the agent calls the model asynchronously")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return await agenerate_from_stream(self._astream(messages, stop, run_manager, **kwargs))

    async def _astream(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> AsyncIterator[ChatGenerationChunk]:
        info = Info(
            tools={t.name: t for t in self.bound if isinstance(t, BaseTool)},
            server_tools=[t for t in self.bound if isinstance(t, dict)],
            schemas=frozenset(t.__name__ for t in self.bound if isinstance(t, type)),
        )
        index = 0
        async for item in self.script(messages, info):
            if isinstance(item, str):
                chunk = AIMessageChunk(content=item)
            elif isinstance(item, Usage):
                chunk = AIMessageChunk(
                    content="",
                    usage_metadata=UsageMetadata(
                        input_tokens=item.input_tokens,
                        output_tokens=item.output_tokens,
                        total_tokens=item.input_tokens + item.output_tokens,
                    ),
                )
            else:
                index += 1
                chunk = AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": item.name,
                            "args": json.dumps(item.args),
                            "id": item.id,
                            "index": index,
                        }
                    ],
                )
            yield ChatGenerationChunk(message=chunk)


def model(script: Script) -> FakeChatModel:
    return FakeChatModel(script=script)


def replies(text: str) -> FakeChatModel:
    async def script(messages, info):
        yield text

    return model(script)


@contextmanager
def use_model(fake: FakeChatModel) -> Iterator[FakeChatModel]:
    """Runs agents on `fake` instead of the configured model."""
    from aios import llm

    with mock.patch.object(llm, "chat_model", lambda name=None: fake):
        yield fake


def briefs(ack: str = "On it.", brief: str = "") -> FakeChatModel:
    """A handoff model (`aios.handoff`) answering with this `{ack, brief}`."""

    async def script(messages, info):
        yield ToolCall("Handoff", {"ack": ack, "brief": brief})

    return model(script)


@contextmanager
def use_handoff(fake: FakeChatModel) -> Iterator[FakeChatModel]:
    """Runs the handoff step on `fake` instead of `briefs()`, which every test has by default."""
    from aios import handoff

    with mock.patch.object(handoff, "model", lambda: fake):
        yield fake


# Where the output a handback summarises says it's from, in its untrusted block.
HANDBACK_OUTPUT = "output"


def untrusted_blocks(messages: list[BaseMessage]) -> list[tuple[str, str]]:
    """Every untrusted block (`aios.jev.wrap`) in the messages, in order: where each says it's
    from, and its content as it was wrapped."""
    from aios.jev import _unescape

    found = []
    for message in messages:
        text = message.content if isinstance(message.content, str) else str(message.content)
        for source, body in re.findall(
            r'<untrusted source="([^"]*)">\n(.*?)\n</untrusted>', text, re.DOTALL
        ):
            found.append((html.unescape(source), _unescape(body)))
    return found


def handback_output(messages: list[BaseMessage]) -> str:
    """The output a handback (`aios.handback`) was asked to summarise, out of its untrusted
    block; empty if it wasn't sent one."""
    blocks = untrusted_blocks(messages)
    return next((body for source, body in blocks if source == HANDBACK_OUTPUT), "")


def summarises(summary: str | None = None) -> FakeChatModel:
    """A handback model (`aios.handback`) answering with `summary`, or else with the output it
    was sent, as it came: so a result reads the same summarised or not, unless a test says."""

    async def script(messages, info):
        yield ToolCall("Handback", {"summary": summary or handback_output(messages)})

    return model(script)


@contextmanager
def use_handback(fake: FakeChatModel) -> Iterator[FakeChatModel]:
    """Runs the handback step on `fake` instead of `summarises()`, which every test has by
    default."""
    from aios import handback

    with mock.patch.object(handback, "model", lambda: fake):
        yield fake


def instructions(messages: list[BaseMessage]) -> str:
    """The agent's own instructions, from the system prompt it was sent: without the rule on
    untrusted blocks every agent's prompt ends with (`aios.jev.UNTRUSTED_RULE`)."""
    from aios.jev import UNTRUSTED_RULE

    system = messages[0].content if messages else ""
    if system == UNTRUSTED_RULE:
        return ""
    return system.removesuffix("\n\n" + UNTRUSTED_RULE)


def last_tool_results(messages: list[BaseMessage]) -> list[ToolMessage]:
    """The tool results at the end of `messages`: those of the model's last tool calls."""
    results = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        results.insert(0, message)
    return results


def tool_output(message: ToolMessage) -> Any:
    """What the tool returned, as the model got it: out of its untrusted block (every tool result
    comes in one, `aios.tool_checks`), and as JSON if it is."""
    from aios.jev import unwrap

    content = unwrap(message.content) if isinstance(message.content, str) else message.content
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def node_input(message: BaseMessage) -> str:
    """A workflow node's input as its agent's model got it, out of the untrusted block its
    predecessors' outputs come in (`aios.workflows.advance.derived`): for scripts that echo
    what each node was given. Anything else is left as it is."""
    from aios.jev import _WRAPPED, unwrap
    from aios.workflows.advance import DERIVED

    text = message.content if isinstance(message.content, str) else str(message.content)
    found = _WRAPPED.match(text)
    if found is not None and text.startswith(f'<untrusted source="{DERIVED}'):
        return unwrap(text)
    return text
