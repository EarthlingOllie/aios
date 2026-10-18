"""The Vercel AI SDK UI message stream protocol, for LangGraph agents.

The frontend's `useChat` sends `UIMessage`s and reads a stream of chunks back (server-sent
events, https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol). This module converts between those
and LangChain messages, and turns a LangGraph agent's `astream(stream_mode=["messages",
"updates"])` output into chunks.

Content arrives either as a plain string or as Anthropic content blocks (text, thinking, server
tool use and results). Tool calls the agent runs come from `tool_call_chunks`, which every
provider fills in.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal
from uuid import uuid4

import psycopg
import sqlalchemy.exc
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

Chunk = dict[str, Any]

HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "cache-control": "no-cache",
    "x-accel-buffering": "no",
}


# --- Requests -----------------------------------------------------------------------------------


class UIPart(BaseModel):
    """A `UIMessage` part. Only the fields the conversion reads; the rest are ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str
    text: str | None = None
    tool_call_id: str | None = Field(None, alias="toolCallId")
    tool_name: str | None = Field(None, alias="toolName")
    state: str | None = None
    input: Any = None
    output: Any = None
    error_text: str | None = Field(None, alias="errorText")
    provider_executed: bool | None = Field(None, alias="providerExecuted")


class UIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    role: Literal["system", "user", "assistant"]
    parts: list[UIPart] = []


class ChatRequest(BaseModel):
    """The body `useChat` posts (`DefaultChatTransport`)."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    trigger: str = "submit-message"
    messages: list[UIMessage] = Field(min_length=1)


def parse_request(body: Any) -> ChatRequest:
    try:
        return ChatRequest.model_validate(body)
    except ValidationError as exc:
        detail = json.loads(exc.json(include_input=False, include_url=False))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from exc


def _text(message: UIMessage) -> str:
    return "".join(p.text or "" for p in message.parts if p.type == "text")


def human_message(message: UIMessage) -> HumanMessage:
    """A user's message, as text: files and other parts aren't accepted from clients.

    It gets a new id: in a LangGraph thread, a message with an id already there replaces that
    message, and the client's id mustn't be able to rewrite history.

    Rejected when blank: the model provider would refuse it, and a run's thread would keep it.
    """
    text = _text(message)
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Expected a message with text")
    return HumanMessage(text, id=str(uuid4()))


def to_langchain(messages: list[UIMessage]) -> list[BaseMessage]:
    """A conversation the client holds, for an agent that doesn't keep history itself.

    Each step of an assistant message becomes a model response with its tool calls, followed by
    the calls' results. Provider-run tools (e.g. web search) are left out: their results can only
    be replayed in the provider's own format.
    """
    out: list[BaseMessage] = []
    for message in messages:
        if message.role == "user":
            out.append(human_message(message))
        elif message.role == "assistant":
            steps: list[list[UIPart]] = [[]]
            for part in message.parts:
                if part.type == "step-start":
                    steps.append([])
                else:
                    steps[-1].append(part)
            for step in steps:
                out.extend(_assistant_step(step))
    return out


def _assistant_step(parts: list[UIPart]) -> list[BaseMessage]:
    text = "".join(p.text or "" for p in parts if p.type == "text")
    calls, results = [], []
    for part in parts:
        if not (part.type.startswith("tool-") or part.type == "dynamic-tool"):
            continue
        if part.provider_executed or not part.tool_call_id:
            continue
        name = part.tool_name or part.type.removeprefix("tool-")
        if part.state == "output-available":
            content = part.output if isinstance(part.output, str) else json.dumps(part.output)
            results.append(ToolMessage(content, tool_call_id=part.tool_call_id, name=name))
        elif part.state == "output-error":
            results.append(
                ToolMessage(
                    part.error_text or "Error",
                    tool_call_id=part.tool_call_id,
                    name=name,
                    status="error",
                )
            )
        else:
            continue  # never ran, so it can't be replayed
        args = part.input if isinstance(part.input, dict) else {}
        calls.append({"name": name, "args": args, "id": part.tool_call_id, "type": "tool_call"})
    if not text and not calls:
        return []
    return [AIMessage(text, tool_calls=calls), *results]


# --- Stored history -> UIMessages ---------------------------------------------------------------


def _blocks(message: AIMessage) -> list[dict[str, Any]]:
    if isinstance(message.content, str):
        return [{"type": "text", "text": message.content}] if message.content else []
    return [b if isinstance(b, dict) else {"type": "text", "text": b} for b in message.content]


def _tool_output(content: Any) -> Any:
    """A tool result as the UI shows it: JSON when it is JSON, otherwise the text."""
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


def _server_result_part(block: dict[str, Any]) -> tuple[str, Any]:
    content = block.get("content")
    if isinstance(content, dict) and content.get("error_code"):
        return "output-error", content["error_code"]
    return "output-available", content


def to_ui_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Stored history as AI SDK `UIMessage`s, so a run renders like a live chat."""
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            out.append(
                {
                    "id": message.id or str(uuid4()),
                    "role": "user",
                    "parts": [{"type": "text", "text": message.text}],
                }
            )
            continue
        if not isinstance(message, AIMessage):
            continue
        if not out or out[-1]["role"] != "assistant":
            out.append({"id": message.id or str(uuid4()), "role": "assistant", "parts": []})
        parts = out[-1]["parts"]
        parts.append({"type": "step-start"})
        server_calls: dict[str, dict[str, Any]] = {}
        for block in _blocks(message):
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                parts.append({"type": "text", "text": block["text"], "state": "done"})
            elif kind == "thinking" and block.get("thinking"):
                parts.append({"type": "reasoning", "text": block["thinking"], "state": "done"})
            elif kind == "server_tool_use":
                part = {
                    "type": f"tool-{block.get('name', 'tool')}",
                    "toolCallId": block.get("id", ""),
                    "state": "input-available",
                    "input": block.get("input") or {},
                    "providerExecuted": True,
                }
                server_calls[part["toolCallId"]] = part
                parts.append(part)
            elif kind and kind.endswith("_tool_result"):
                part = server_calls.get(block.get("tool_use_id", ""))
                if part is not None:
                    state, value = _server_result_part(block)
                    part["state"] = state
                    part["errorText" if state == "output-error" else "output"] = value
        for call in message.tool_calls:
            part = {
                "type": f"tool-{call['name']}",
                "toolCallId": call["id"],
                "state": "input-available",
                "input": call["args"],
            }
            result = results.get(call["id"])
            if result is not None and result.status == "error":
                part |= {"state": "output-error", "errorText": str(result.content)}
            elif result is not None:
                part |= {"state": "output-available", "output": _tool_output(result.content)}
            parts.append(part)
    return out


# --- Streaming ----------------------------------------------------------------------------------


def error_text(exc: BaseException) -> str:
    """What to show the user of an error that ended an agent's turn.

    A database error's text names the server and user (and SQLAlchemy's, the statement), so it
    stays in the log. Others, mostly the model's, are shown as they are.
    """
    if isinstance(exc, psycopg.Error | sqlalchemy.exc.SQLAlchemyError):
        return "Database error"
    return str(exc) or exc.__class__.__name__


class UIStream:
    """Turns one agent run's stream into UI message chunks, and keeps what's needed to save a
    reply that didn't finish (`partial_reply`)."""

    def __init__(self) -> None:
        self._step_open = False
        # The text or reasoning part being streamed: (kind, key, part id).
        self._open: tuple[str, tuple[str, Any], str] | None = None
        # Tool calls being streamed, by (message id, index) -> tool call id.
        self._calls: dict[tuple[str, Any], str] = {}
        # Provider-run tool calls being streamed, by (message id, index) -> [id, name, json].
        self._server_calls: dict[tuple[str, Any], list[str]] = {}
        # The model response being streamed, until the graph reports it complete.
        self._reply: AIMessageChunk | None = None
        self.finish_reason = "stop"

    def start(self) -> Iterator[Chunk]:
        yield {"type": "start"}

    def feed(self, mode: str, data: Any) -> Iterator[Chunk]:
        if mode == "messages":
            message, _ = data
            if isinstance(message, AIMessageChunk):
                yield from self._model_chunk(message)
        elif mode == "updates":
            for node, update in data.items():
                if node == "__interrupt__":
                    # The run paused (e.g. a schema change request waiting for the user): its
                    # tool call has no result until it's resumed.
                    continue
                for message in (update or {}).get("messages", []):
                    if isinstance(message, AIMessage) and node == "model":
                        yield from self._model_done(message)
                    elif isinstance(message, ToolMessage):
                        yield from self._tool_result(message)

    def error(self, exc: BaseException) -> Iterator[Chunk]:
        yield from self._close_part()
        self.finish_reason = "error"
        yield {"type": "error", "errorText": error_text(exc)}

    def finish(self) -> Iterator[Chunk]:
        yield from self._close_part()
        if self._step_open:
            self._step_open = False
            yield {"type": "finish-step"}
        yield {"type": "finish", "finishReason": self.finish_reason}

    def partial_reply(self) -> AIMessage | None:
        """The text of a model response cut off mid-stream, to keep in the history.

        Tool calls and provider tool use are dropped: they're incomplete, and a call without its
        result would make the next model request invalid.
        """
        if self._reply is None:
            return None
        text = "".join(
            b.get("text", "")
            for b in _blocks(AIMessage(content=self._reply.content))
            if b.get("type") == "text"
        )
        return AIMessage(text, id=self._reply.id) if text else None

    # --- internals ---

    def _start_step(self) -> Iterator[Chunk]:
        if not self._step_open:
            self._step_open = True
            yield {"type": "start-step"}

    def _close_part(self) -> Iterator[Chunk]:
        if self._open is not None:
            kind, _, part_id = self._open
            self._open = None
            yield {"type": f"{kind}-end", "id": part_id}

    def _delta(self, kind: str, key: tuple[str, Any], delta: str) -> Iterator[Chunk]:
        if not delta:
            return
        if self._open is None or self._open[:2] != (kind, key):
            yield from self._close_part()
            part_id = str(uuid4())
            self._open = (kind, key, part_id)
            yield {"type": f"{kind}-start", "id": part_id}
        yield {"type": f"{kind}-delta", "id": self._open[2], "delta": delta}

    def _model_chunk(self, chunk: AIMessageChunk) -> Iterator[Chunk]:
        msg_id = chunk.id or ""
        if self._reply is None or self._reply.id != chunk.id:
            self._reply = chunk
        else:
            self._reply = self._reply + chunk
        yield from self._start_step()

        if isinstance(chunk.content, str):
            yield from self._delta("text", (msg_id, "text"), chunk.content)
        else:
            for block in chunk.content:
                if isinstance(block, str):
                    yield from self._delta("text", (msg_id, "text"), block)
                    continue
                kind, index = block.get("type"), block.get("index")
                key = (msg_id, index)
                if kind == "text":
                    yield from self._delta("text", key, block.get("text", ""))
                elif kind == "thinking":
                    yield from self._delta("reasoning", key, block.get("thinking", ""))
                elif kind == "server_tool_use":
                    yield from self._close_part()
                    self._server_calls[key] = [block.get("id", ""), block.get("name", ""), ""]
                    yield {
                        "type": "tool-input-start",
                        "toolCallId": block.get("id", ""),
                        "toolName": block.get("name", ""),
                        "providerExecuted": True,
                    }
                elif kind == "input_json_delta" and key in self._server_calls:
                    self._server_calls[key][2] += block.get("partial_json", "")
                elif kind and kind.endswith("_tool_result"):
                    yield from self._server_result(msg_id, block)

        for call in chunk.tool_call_chunks:
            key = (msg_id, call.get("index"))
            if call.get("id") and key not in self._calls:
                yield from self._close_part()
                self._calls[key] = call["id"]
                yield {
                    "type": "tool-input-start",
                    "toolCallId": call["id"],
                    "toolName": call.get("name") or "",
                }
            if call.get("args") and key in self._calls:
                yield {
                    "type": "tool-input-delta",
                    "toolCallId": self._calls[key],
                    "inputTextDelta": call["args"],
                }

    def _server_result(self, msg_id: str, block: dict[str, Any]) -> Iterator[Chunk]:
        call_id = block.get("tool_use_id", "")
        for key, (sid, name, args) in list(self._server_calls.items()):
            if sid == call_id and key[0] == msg_id:
                del self._server_calls[key]
                yield {
                    "type": "tool-input-available",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": _tool_output(args) if args else {},
                    "providerExecuted": True,
                }
        state, value = _server_result_part(block)
        if state == "output-error":
            yield {"type": "tool-output-error", "toolCallId": call_id, "errorText": str(value)}
        else:
            yield {
                "type": "tool-output-available",
                "toolCallId": call_id,
                "output": value,
                "providerExecuted": True,
            }

    def _model_done(self, message: AIMessage) -> Iterator[Chunk]:
        streamed = self._reply is not None and self._reply.id == message.id
        self._reply = None
        yield from self._start_step()
        if not streamed:
            # The model wasn't streamed (e.g. it doesn't support it): send the whole response.
            for block in _blocks(message):
                if block.get("type") == "text":
                    yield from self._delta("text", (message.id or "", "text"), block["text"])
        yield from self._close_part()
        for call in message.tool_calls:
            yield {
                "type": "tool-input-available",
                "toolCallId": call["id"],
                "toolName": call["name"],
                "input": call["args"],
            }
        reason = (message.response_metadata or {}).get("stop_reason")
        self.finish_reason = {"max_tokens": "length", "refusal": "content-filter"}.get(
            reason, "stop"
        )
        self._calls = {k: v for k, v in self._calls.items() if k[0] != message.id}
        self._step_open = False
        yield {"type": "finish-step"}

    def _tool_result(self, message: ToolMessage) -> Iterator[Chunk]:
        if message.status == "error":
            yield {
                "type": "tool-output-error",
                "toolCallId": message.tool_call_id,
                "errorText": str(message.content),
            }
        else:
            yield {
                "type": "tool-output-available",
                "toolCallId": message.tool_call_id,
                "output": _tool_output(message.content),
            }


def encode(chunk: Chunk) -> str:
    return f"data: {json.dumps(chunk, separators=(',', ':'), default=str)}\n\n"


def streaming_response(chunks: AsyncIterator[Chunk]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        async for chunk in chunks:
            yield encode(chunk)
        yield "data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream", headers=HEADERS)
