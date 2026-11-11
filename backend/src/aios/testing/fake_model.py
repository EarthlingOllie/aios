"""A scripted chat model, standing in for the provider's: the backend tests script it per test
(`use_model`, `use_handoff`), and a running app can name a scenario of it as its model
(`aios.testing.scenarios`, with scripted models enabled)."""

import json
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


def last_tool_results(messages: list[BaseMessage]) -> list[ToolMessage]:
    """The tool results at the end of `messages`: those of the model's last tool calls."""
    results = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        results.insert(0, message)
    return results


def tool_output(message: ToolMessage) -> Any:
    try:
        return json.loads(message.content)
    except ValueError:
        return message.content
