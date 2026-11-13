"""Compaction: long conversations keep working once they outgrow what the model can read.

Before each model call of a run (`CompactionMiddleware`, on every agent, `aios.runner`), the
run's latest summary (`aios.datastores.models.RunSummary`) stands in for the turns it covers in
what the model is sent. If the model's input still passes `COMPACTION_THRESHOLD_TOKENS`
(estimated), the oldest whole turns beyond the latest `COMPACTION_TURNS_KEPT` are folded into a
new summary, together with the previous one, by one call to the handoff model (`aios.handoff`),
of at most about `COMPACTION_SUMMARY_TOKENS` tokens; the call goes on with it.

- **Turns, as the model sees them,** begin at a person's message (a `HumanMessage`), so the cut
  is only ever made just before one: a tool call and its result are never parted, and the
  provider accepts the history. A turn carrying on after an approval is part of the one it
  carries on. A summary records the first message it doesn't cover, and the last turn record it
  covers (`aios.turns`, placed by `Turn.message_end`).
- **The history is never altered:** the checkpointed messages, the run page and search keep
  every turn (`aios.search_index`). Only the model's working view is shortened.
- **Derived text:** a summary is written from untrusted content, so it carries the verdict
  `derived` and reaches the model wrapped as untrusted (`aios.jev.wrap`), as what it's written
  from reaches the summarising model.
- **Usage and tracing:** each compaction is a usage ledger entry with the purpose `compaction`,
  charged as the turn it happened in is (`aios.usage`), and traced as a child of the turn's run,
  tagged `compaction` (`aios.tracing`).
- **Failure:** a summary that can't be written is logged, and the call goes on with the view
  as it was; one that can't be read (the store isn't ready or can't be reached) is logged, and
  the call goes on as it is.

Retention prunes a conversation run turn by turn, and deletes a summary once every turn it
covers is gone (`aios.retention`).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables.config import ensure_config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios import jev, llm, model_rate_limit, usage
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import RunSummary, Turn
from aios.models import UsagePurpose

log = logging.getLogger(__name__)

# Where a summary says it's from, in its untrusted block: derived text.
SOURCE = "derived:compaction"
# Where the turns being folded say they're from, for the summarising model.
FOLDED_SOURCE = "run"
# About how many characters a token is, to hold a summary to its size.
CHARS_PER_TOKEN = 4
# How much of a tool call's arguments or result the summarising model is shown.
TOOL_TEXT_LIMIT = 2_000
# How long the summarising call may take.
TIMEOUT_SECONDS = 120.0
TAG = "compaction"

SYSTEM_PROMPT = """\
You keep a running summary of a long conversation between people and an AI agent, so the agent \
can carry on without rereading its oldest turns. You're given the summary so far (if there is \
one) and the turns that come after it, oldest first. Write one new summary covering both.

Keep what the agent will need: what was asked and decided, facts, names, numbers, links, \
results of tool calls that matter, commitments and open questions. Drop pleasantries and \
anything superseded. Write plain prose or short bullets, in the third person, at most about \
{words} words. Invent nothing."""

SYSTEM_PROMPT += "\n\n" + jev.UNTRUSTED_RULE


def model() -> BaseChatModel:
    """The summarising model: the handoff's (`HANDOFF_MODEL`, or the deployment's default)."""
    return llm.chat_model(get_settings().handoff_model or None)


def model_name() -> str:
    return llm.model_name(get_settings().handoff_model or None)


def wrapped(text: str) -> str:
    """A summary as the model reads it: in an untrusted block, as derived text."""
    return jev.wrap(text, SOURCE)


@dataclass(frozen=True)
class Latest:
    """A run's latest summary: what it says, and the first message it doesn't cover."""

    text: str
    kept_from_message_id: str


def latest(store: Session, run_id: int) -> Latest | None:
    row = store.execute(
        select(RunSummary.text, RunSummary.kept_from_message_id)
        .where(RunSummary.agent_run_id == run_id)
        .order_by(RunSummary.id.desc())
        .limit(1)
    ).one_or_none()
    return None if row is None else Latest(*row)


def _latest(data_store_id: uuid.UUID, run_id: int) -> Latest | None:
    with store_session(data_store_id, "ro") as store:
        return latest(store, run_id)


def uncovered(messages: Sequence[BaseMessage], summary: Latest | None) -> int | None:
    """Where the messages the summary doesn't cover begin; None if it covers none of these
    (there's none, or its first uncovered message isn't among them)."""
    if summary is None:
        return None
    for i, message in enumerate(messages):
        if message.id == summary.kept_from_message_id:
            return i
    return None


def turn_starts(messages: Sequence[BaseMessage]) -> list[int]:
    """Where each turn begins, as the model sees them: at each person's message (and at the
    start, whatever the first message is)."""
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    return starts


def _clip(text: str, limit: int = TOOL_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + " [...]"


def transcript(messages: Sequence[BaseMessage]) -> str:
    """The turns being folded, as text for the summarising model."""
    lines = []
    for message in messages:
        text = jev.unwrap(message.text) if isinstance(message, ToolMessage) else message.text
        if isinstance(message, HumanMessage):
            lines.append(f"Person: {text}")
        elif isinstance(message, AIMessage):
            if text.strip():
                lines.append(f"Agent: {text}")
            for call in message.tool_calls:
                args = json.dumps(call.get("args") or {}, default=str)
                lines.append(f"Agent called {call.get('name')}: {_clip(args)}")
        elif isinstance(message, ToolMessage):
            lines.append(f"Result of {message.name or 'the tool'}: {_clip(text)}")
        elif text.strip():
            lines.append(f"{message.type}: {text}")
    return "\n\n".join(lines)


def request_messages(previous: str | None, folded: Sequence[BaseMessage]) -> list[BaseMessage]:
    """What the summarising model is sent: the previous summary and the turns to fold, each
    wrapped as untrusted."""
    words = get_settings().compaction_summary_tokens * 3 // 4
    parts = [
        "The summary so far:",
        wrapped(previous) if previous else "(none: these are the conversation's first turns)",
        "The turns that come after it, oldest first:",
        jev.wrap(transcript(folded), FOLDED_SOURCE),
    ]
    return [SystemMessage(SYSTEM_PROMPT.format(words=words)), HumanMessage("\n\n".join(parts))]


async def summarise(
    previous: str | None, folded: Sequence[BaseMessage], metadata: dict
) -> tuple[str, AIMessage | None]:
    """The new summary, and the response it came in (for its usage)."""
    size = get_settings().compaction_summary_tokens
    name = model_name()
    # Paced by the model's rate limit, as a turn's calls are (`aios.model_rate_limit`).
    await model_rate_limit.acquire(name)
    response = (
        await model()
        .bind(max_tokens=size)
        .ainvoke(
            request_messages(previous, folded),
            {"run_name": TAG, "tags": [TAG], "metadata": metadata},
        )
    )
    await model_rate_limit.spend(name, model_rate_limit.tokens_of(response))
    text = response.text.strip() if isinstance(response, AIMessage) else ""
    return text[: size * CHARS_PER_TOKEN], response if isinstance(response, AIMessage) else None


def last_turn_covered(
    store: Session, run_id: int, position: int, current_turn_id: int | None
) -> int | None:
    """The run's last turn whose messages all come before `position` in its history; failing
    that (turns from before their ends were kept), the turn before the current one."""
    found = store.scalar(
        select(func.max(Turn.id)).where(
            Turn.agent_run_id == run_id,
            Turn.message_end.is_not(None),
            Turn.message_end <= position,
        )
    )
    if found is None and current_turn_id is not None:
        found = store.scalar(
            select(func.max(Turn.id)).where(Turn.agent_run_id == run_id, Turn.id < current_turn_id)
        )
    return found


def _keep(
    data_store_id: uuid.UUID,
    run_id: int,
    *,
    position: int,
    kept_from_message_id: str,
    text: str,
    charge: usage.Charge | None,
    response: AIMessage | None,
) -> None:
    """Keep the new summary and its call's usage, in one transaction."""
    with store_session(data_store_id) as store:
        turn_id = charge.turn_id if charge is not None else None
        store.add(
            RunSummary(
                agent_run_id=run_id,
                last_turn_id=last_turn_covered(store, run_id, position, turn_id),
                kept_from_message_id=kept_from_message_id,
                text=text,
            )
        )
        if response is not None:
            charge = charge or usage.for_run(store, run_id)
            if charge is not None:
                usage.record_model_call(
                    store, charge.but(purpose=UsagePurpose.compaction), model_name(), response
                )
        store.commit()


def _input_tokens(system: SystemMessage | None, messages: Sequence[BaseMessage]) -> int:
    return count_tokens_approximately([*([system] if system else []), *messages])


def _trace_metadata(run_id: int, turn_id: int | None) -> dict:
    """The summarising call's metadata: the turn's run's, tagged as a compaction."""
    parent = ensure_config()
    metadata = dict(parent.get("metadata") or {})
    tags = [*(metadata.get("langfuse_tags") or []), TAG]
    return metadata | {"langfuse_tags": tags, "agent_run_id": run_id, "turn_id": turn_id}


async def compacted(
    data_store_id: uuid.UUID,
    run_id: int,
    system: SystemMessage | None,
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """The messages the model is sent for this call of the run: its latest summary in place of
    the turns it covers, after folding in more turns if the input passes the threshold."""
    settings = get_settings()
    try:
        summary = await run_in_threadpool(_latest, data_store_id, run_id)
    except Exception:
        # The store can't be read (not ready, unreachable): the call goes on as it is, and the
        # tools say why the store can't be used.
        log.exception("couldn't read run %s's summary", run_id)
        return messages
    start = uncovered(messages, summary)
    previous = summary.text if summary is not None and start is not None else None
    body = messages[start:] if start is not None else messages
    view = [HumanMessage(wrapped(previous)), *body] if previous is not None else body
    if _input_tokens(system, view) <= settings.compaction_threshold_tokens:
        return view
    starts = turn_starts(body)
    if len(starts) <= settings.compaction_turns_kept:
        return view
    cut = starts[len(starts) - settings.compaction_turns_kept]
    kept_from = body[cut].id
    if not kept_from:
        return view
    charge = usage.current()
    turn_id = charge.turn_id if charge is not None else None
    try:
        text, response = await asyncio.wait_for(
            summarise(previous, body[:cut], _trace_metadata(run_id, turn_id)), TIMEOUT_SECONDS
        )
        if not text:
            log.warning("compaction of run %s gave no summary", run_id)
            return view
        await run_in_threadpool(
            _keep,
            data_store_id,
            run_id,
            position=len(messages) - len(body) + cut,
            kept_from_message_id=kept_from,
            text=text,
            charge=charge,
            response=response,
        )
    except Exception:
        log.exception("couldn't compact run %s", run_id)
        return view
    return [HumanMessage(wrapped(text)), *body[cut:]]


class CompactionMiddleware(AgentMiddleware):
    """On every agent (`aios.runner.build_agent`): before each model call of a run, its latest
    summary in place of the turns it covers, folding in more once the input passes the
    threshold (`compacted`). A call outside a run is sent as it is."""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        context = request.runtime.context if request.runtime is not None else None
        run_id = getattr(context, "run_id", None)
        data_store_id = getattr(context, "data_store_id", None)
        if run_id is None or data_store_id is None:
            return await handler(request)
        messages = await compacted(
            data_store_id, run_id, request.system_message, list(request.messages)
        )
        return await handler(request.override(messages=messages))
