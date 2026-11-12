"""The handback: a result coming back to a chat is summarised before it's posted.

Shaped like the handoff (`aios.handoff`): one structured call to the handback model
(`HANDBACK_MODEL`, else the handoff's, else the deployment's `LLM_MODEL`) returning `{summary}`.

**When.** Where a run ends, `aios.invocations` decides whether its result is handed back: a
mentioned workflow's result, every delivery (a trigger's run posting to a chat), and an agent's
reply to a mention that's longer than `handback_threshold` characters or parses as JSON. An
agent's short reply is posted as written. A run handed back is marked `handing_back`, with its
full output, in the transaction that ends it, and a handback item is enqueued on the run
service's interactive lane once that commits (`after_store_commit`); the requeue sweep puts back
one whose enqueue was lost (`aios.run_service.work.lost`).

**The call** (`hand_back`, the item's handler) is sent the asking message (the mention, the
trigger's message, or the input an agent started a workflow with, `start_workflow`), the brief,
and the output cut to `handback_input_limit` characters keeping its start and end, each in an
untrusted block (`aios.jev.wrap`): all are people's text or derived from it. It answers in
about 150 words, in the voice of the agent or workflow.

**Posting.** The summary is posted as the result (`aios.invocations.post_handback`), stored on
the invocation, which succeeds. The full output stays on the invocation, behind the card's
disclosure. A handback that fails (the model errs, times out, or answers with no summary) posts
the raw output cut to `handback_raw_limit` characters, with a note, and records why; the
invocation still succeeds. So does an item whose attempts run out (`give_up`).

Each call is traced to Langfuse (`aios.tracing`), tagged `handback`, and recorded in the usage
ledger with purpose handback (`aios.usage`).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.concurrency import run_in_threadpool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import invocations, jev, llm, model_rate_limit, tracing, usage
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Agent,
    ChatMessage,
    Invocation,
    Trigger,
    Workflow,
    WorkflowRun,
)
from aios.db import SessionLocal
from aios.models import DataStore, InvocationCause, InvocationStatus, UsagePurpose

log = logging.getLogger(__name__)

# Where each part of what the handback is sent says it's from, in its untrusted block.
ASKING = invocations.CHAT_MESSAGE
SCHEDULED = "trigger message"
STARTED_WITH = "start_workflow input"
BRIEF = "brief"
OUTPUT = "output"

# Between the start and the end of an output cut to fit.
CUT = "\n\n[…]\n\n"

FAILED_NOTE = "_Couldn't summarise this result ({error}), so it's posted as it came{cut}._"
GAVE_UP = "the service stopped while summarising it"

SYSTEM_PROMPT = """\
You hand a result back to a group chat. The {kind} "{name}" has finished a run, and its \
output is about to be posted in the chat as its reply. Write that reply: a summary of the \
output, answering what was asked, in at most about 150 words.

- Write as {name}, in the first person, addressed to the chat.
- Keep the names, numbers and links that matter, exactly as the output gives them.
- Invent nothing: say only what the output says. If it says the work failed or is incomplete, \
say so.
- Markdown is fine. Don't mention that this is a summary; the full output is one click away."""

SYSTEM_PROMPT += "\n\n" + jev.UNTRUSTED_RULE


class Handback(BaseModel):
    """The reply to post in place of the run's output."""

    summary: str = Field(description="The output summarised, as the agent or workflow.")


def model() -> BaseChatModel:
    """The handback's model: `HANDBACK_MODEL`, else the handoff's, else the deployment's."""
    return llm.chat_model(model_setting())


def model_setting() -> str | None:
    settings = get_settings()
    return settings.handback_model or settings.handoff_model or None


def model_name() -> str:
    """The handback model's name, as the price table knows it."""
    return llm.model_name(model_setting())


def cut(text: str, limit: int) -> str:
    """`text`, cut to about `limit` characters keeping its start and its end."""
    if len(text) <= limit:
        return text
    half = max(0, (limit - len(CUT)) // 2)
    return text[:half] + CUT + text[len(text) - half :]


def raw(output: str, error: str) -> str:
    """What's posted when summarising failed: the output, cut short, with a note saying why."""
    limit = get_settings().handback_raw_limit
    shortened = len(output) > limit
    body = output[:limit].rstrip() + "…" if shortened else output
    note = FAILED_NOTE.format(error=error, cut=", cut short" if shortened else "")
    return f"{body}\n\n{note}" if body.strip() else note


@dataclass(frozen=True)
class Request:
    """What one handback is sent, and what its trace is tagged with, as plain values."""

    system: str
    content: str
    output: str
    metadata: dict[str, Any]
    charge: usage.Charge


def _organisation(data_store_id: uuid.UUID) -> int | None:
    with SessionLocal() as db:
        return db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )


def _asking(store: Session, invocation: Invocation) -> tuple[str, str] | None:
    """What the run was asked, and where that's from: the mentioning message, the trigger's
    message for a trigger's run, or the input an agent started it with (`start_workflow`)."""
    if invocation.message_id is not None:
        body = store.scalar(select(ChatMessage.body).where(ChatMessage.id == invocation.message_id))
        if body is not None:
            return invocations.message_text(body), ASKING
    if invocation.trigger_id is not None:
        message = store.scalar(select(Trigger.input).where(Trigger.id == invocation.trigger_id))
        if message:
            return message, SCHEDULED
    if invocation.cause == InvocationCause.start_workflow and invocation.workflow_run_id:
        given = store.scalar(
            select(WorkflowRun.input).where(WorkflowRun.id == invocation.workflow_run_id)
        )
        if given:
            return given, STARTED_WITH
    return None


def _begin(data_store_id: uuid.UUID, invocation_id: int) -> Request | None:
    """What the store's invocation's handback is sent; None unless it's handing back."""
    organisation_id = _organisation(data_store_id)
    with store_session(data_store_id, "ro") as store:
        invocation = store.get(Invocation, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.handing_back:
            return None
        is_agent = invocation.agent_id is not None
        target_id = invocation.agent_id if is_agent else invocation.workflow_id
        target = Agent if is_agent else Workflow
        name = store.scalar(select(target.name).where(target.id == target_id)) or ""
        output = invocation.full_output or ""
        parts = []
        if (asking := _asking(store, invocation)) is not None:
            text, source = asking
            parts += ["What it was asked:", jev.wrap(text, source)]
        if invocation.brief:
            parts += ["What it was told of the chat:", jev.wrap(invocation.brief, BRIEF)]
        limit = get_settings().handback_input_limit
        parts += [f"{name}'s output:", jev.wrap(cut(output, limit), OUTPUT)]
        chat_id = invocation.chat_id or invocation.delivery_chat_id
        return Request(
            system=SYSTEM_PROMPT.format(kind=invocation.target_kind.value, name=name),
            content="\n\n".join(parts),
            output=output,
            charge=usage.Charge(
                data_store_id,
                UsagePurpose.handback,
                agent_id=invocation.agent_id,
                workflow_id=invocation.workflow_id,
                agent_run_id=invocation.agent_run_id,
                workflow_run_id=invocation.workflow_run_id,
                invocation_id=invocation.id,
                person_id=usage.person_of(invocation),
                cause=invocation.cause,
            ),
            metadata=tracing.trace_metadata(
                session_id=f"chat-{organisation_id}-{chat_id}" if chat_id else None,
                organisation_id=organisation_id,
                created_by=invocation.requested_by,
                tags=["handback"],
                invocation_id=invocation.id,
                chat_id=chat_id,
                target_kind=invocation.target_kind.value,
                target_id=target_id,
                target_name=name,
            ),
        )


async def _ask(request: Request) -> tuple[Handback | None, AIMessage | None]:
    """The handback, and the model's response it came in (for its usage)."""
    structured = model().with_structured_output(Handback, include_raw=True)
    name = model_name()
    await model_rate_limit.acquire(name)
    answer = await structured.ainvoke(
        [SystemMessage(request.system), HumanMessage(request.content)],
        {"callbacks": tracing.callbacks(), "run_name": "handback", "metadata": request.metadata},
    )
    found = answer.get("raw")
    found = found if isinstance(found, AIMessage) else None
    await model_rate_limit.spend(name, model_rate_limit.tokens_of(found))
    return answer.get("parsed"), found


def _record(
    data_store_id: uuid.UUID,
    invocation_id: int,
    summary: str | None,
    error: str | None,
    output: str,
    charge: usage.Charge | None,
    response: AIMessage | None,
) -> None:
    """Post the summary, or the raw output with why summarising failed, and the call's usage,
    in one transaction."""
    with store_session(data_store_id) as store:
        if summary is not None:
            invocations.post_handback(store, invocation_id, summary, summary=summary)
        else:
            invocations.post_handback(store, invocation_id, raw(output, error or ""), error=error)
        if response is not None and charge is not None:
            usage.record_model_call(store, charge, model_name(), response)
        store.commit()


async def hand_back(data_store_id: uuid.UUID, invocation_id: int) -> None:
    """Run the store's invocation's handback, and post what it gives (see the module
    docstring). Nothing for one that isn't handing back."""
    request = await run_in_threadpool(_begin, data_store_id, invocation_id)
    if request is None:
        return
    summary: str | None = None
    error: str | None = None
    response: AIMessage | None = None
    try:
        answer, response = await asyncio.wait_for(
            _ask(request), get_settings().handback_timeout_seconds
        )
        summary = answer.summary.strip() if answer is not None else None
        if not summary:
            summary, error = None, "the model gave no summary"
    except TimeoutError:
        error = "it took too long"
    except Exception as exc:
        log.warning("the handback for invocation %s failed", invocation_id, exc_info=True)
        error = str(exc) or type(exc).__name__
    await run_in_threadpool(
        _record,
        data_store_id,
        invocation_id,
        summary,
        error,
        request.output,
        request.charge,
        response,
    )


def _give_up(data_store_id: uuid.UUID, invocation_id: int) -> None:
    with store_session(data_store_id) as store:
        output = store.scalar(
            select(Invocation.full_output).where(
                Invocation.id == invocation_id,
                Invocation.status == InvocationStatus.handing_back,
            )
        )
        if output is not None:
            invocations.post_handback(store, invocation_id, raw(output, GAVE_UP), error=GAVE_UP)
        store.commit()


async def give_up(data_store_id: uuid.UUID, invocation_id: int) -> None:
    """The handback's attempts ran out (its processes died): post the raw output, with a note."""
    await run_in_threadpool(_give_up, data_store_id, invocation_id)
