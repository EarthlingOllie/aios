"""The handoff: before a mention's run starts, brief it on the chat and say what was understood.

A run started from a chat sees only its input, so the member mentioned would otherwise have to be
told the context again. Before the run starts (`brief`, the first step of its firing), the
invocation goes `briefing` and one structured call to the handoff model (`HANDOFF_MODEL`, by
default the deployment's `LLM_MODEL`) reads a bounded window of the chat up to the mentioning
message (`window`: the last `HANDOFF_WINDOW_MESSAGES` messages or about `HANDOFF_WINDOW_TOKENS`
tokens of them, whichever is less) and returns `{ack, brief}`:

- the ack, a line in the member's voice saying what it understood it's being asked to do, which
  replaces the placeholder in its acknowledgement so whoever asked can correct it early;
- the brief, the context the run needs that the message doesn't say, kept on the invocation and
  prepended to the run's input (`aios.invocations.run_input`).

It runs for agents and workflows alike, and is never skipped. A handoff that fails (the model
errs, times out or answers with no `{ack, brief}`) fails the invocation with why, as a refused
start does (`aios.firing.CantStart`). Each call is traced to Langfuse (`aios.tracing`), tagged
`handoff`, with the invocation it was for, the organisation and the member who mentioned it.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.concurrency import run_in_threadpool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aios import invocations, llm, tracing
from aios.config import get_settings
from aios.datastores import people, store_session
from aios.datastores.models import Agent, ChatMessage, Invocation, Workflow
from aios.db import SessionLocal
from aios.firing import CantStart
from aios.models import ChatAuthorKind, ChatMessageKind, DataStore

log = logging.getLogger(__name__)

# How long the handoff's model call may take: well within the time a workflow invocation may
# take to start before it counts as lost (`aios.invocations.START_WITHIN`).
TIMEOUT_SECONDS = 60.0
# About how many characters a token is, for the window's budget: an estimate, not a count.
CHARS_PER_TOKEN = 4

SYSTEM_PROMPT = """\
You hand work over in a group chat. A member of the chat, the {kind} "{name}", has just been \
mentioned, and a run of it will start with the mentioning message as its input. The run sees \
nothing else of the chat. From the chat you're shown, return:

- ack: one short line in the first person, as {name}, saying what you understand you're being \
asked to do, so the person who asked can correct it if it's wrong. For example: "On it: I'll \
check the Q3 figures against the plan." No greeting, and no promises about timing.
- brief: what the run needs from the chat to do this well that the message doesn't already \
say: facts, decisions, constraints, names, links and earlier results. Plain prose or short \
bullets, at most about 200 words. Don't restate the message, address no one, and invent \
nothing. Leave it empty if the chat holds nothing relevant beyond the message."""


class Handoff(BaseModel):
    """What the run is being asked to do, and what it needs to know from the chat."""

    ack: str = Field(description="One line, as the member, saying what it understood.")
    brief: str = Field(description="The chat's context the run needs; empty if none.")


def model() -> BaseChatModel:
    """The handoff's model: `HANDOFF_MODEL`, or the deployment's default."""
    return llm.chat_model(get_settings().handoff_model or None)


@dataclass(frozen=True)
class Request:
    """What one handoff is sent, and what its trace is tagged with, as plain values."""

    system: str
    chat: str
    metadata: dict[str, Any]


# The names of the people, agents and workflows that posted messages, by (kind, id).
Names = dict[tuple[ChatAuthorKind, int], str]


def author_name(message: ChatMessage, names: Names) -> str:
    """Who posted the message, as the transcript names them, by its name in `names` (a person
    through the people mirror, an agent or workflow from the organisation's store)."""
    if message.author_kind == ChatAuthorKind.system:
        return "system"
    author_id = message.author_user_id or message.author_agent_id or message.author_workflow_id
    name = names.get((message.author_kind, author_id)) if author_id is not None else None
    if name is None:
        return f"a deleted {message.author_kind}"
    return f"{name} ({message.author_kind})"


def line(message: ChatMessage, names: Names, limit: int | None = None) -> str:
    """The message as a line of the transcript, its text cut to `limit` characters."""
    text = invocations.message_text(message.body)
    if limit is not None and len(text) > limit:
        text = text[:limit] + "…"
    return f"[{author_name(message, names)}]: {text}"


def _names(store: Session, organisation_id: int, messages: list[ChatMessage]) -> Names:
    names: Names = {}
    for kind, model in ((ChatAuthorKind.agent, Agent), (ChatAuthorKind.workflow, Workflow)):
        ids = {m.author_agent_id or m.author_workflow_id for m in messages if m.author_kind == kind}
        if ids:
            for i, name in store.execute(select(model.id, model.name).where(model.id.in_(ids))):
                names[kind, i] = name
    user_ids = [m.author_user_id for m in messages if m.author_user_id is not None]
    if user_ids:
        with SessionLocal() as db:
            for i, person in people.resolve(db, organisation_id, user_ids).items():
                names[ChatAuthorKind.user, i] = person.name
    return names


def window(store: Session, organisation_id: int, invocation: Invocation) -> list[str]:
    """The chat up to the mentioning message as transcript lines, oldest first: its last
    `handoff_window_messages` messages (the run cards' acknowledgements aside), or as many of
    those as fit in about `handoff_window_tokens` tokens, whichever is fewer. The mentioning
    message is always the last, cut to fit if it alone is over the budget."""
    settings = get_settings()
    rows = store.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.chat_id == invocation.chat_id,
            ChatMessage.id <= invocation.message_id,
            ChatMessage.kind == ChatMessageKind.text,
        )
        .order_by(ChatMessage.id.desc())
        .limit(settings.handoff_window_messages)
    ).all()
    budget = settings.handoff_window_tokens * CHARS_PER_TOKEN
    names = _names(store, organisation_id, list(rows))
    mentioning, *earlier = rows
    lines = [line(mentioning, names, limit=budget)]
    budget -= len(lines[0])
    for message in earlier:
        text = line(message, names)
        if len(text) > budget:
            break
        lines.append(text)
        budget -= len(text)
    lines.reverse()
    return lines


def _organisation(data_store_id: uuid.UUID) -> int | None:
    with SessionLocal() as db:
        return db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )


def _begin(data_store_id: uuid.UUID, invocation_id: int) -> Request | None:
    """Mark the store's invocation briefing, and gather what its handoff is sent; None if it
    isn't queued any more (it has finished since)."""
    organisation_id = _organisation(data_store_id)
    with store_session(data_store_id) as store:
        invocation = invocations.begin_briefing(store, invocation_id)
        if invocation is None:
            return None
        is_agent = invocation.agent_id is not None
        target_id = invocation.agent_id if is_agent else invocation.workflow_id
        model = Agent if is_agent else Workflow
        name = store.scalar(select(model.name).where(model.id == target_id)) or ""
        *earlier, mentioning = window(store, organisation_id, invocation)
        chat = "\n\n".join(
            [
                "The chat so far, oldest first:",
                *(earlier or ["(nothing before the message)"]),
                f"The message mentioning {name}:",
                mentioning,
            ]
        )
        request = Request(
            system=SYSTEM_PROMPT.format(kind=invocation.target_kind.value, name=name),
            chat=chat,
            metadata=tracing.trace_metadata(
                # A chat's handoffs, together (chat ids are each store's own).
                session_id=f"chat-{organisation_id}-{invocation.chat_id}",
                # As the run's own trace will say: the organisation it runs for, and who asked.
                organisation_id=organisation_id,
                created_by=invocation.requested_by,
                tags=["handoff"],
                invocation_id=invocation.id,
                chat_id=invocation.chat_id,
                message_id=invocation.message_id,
                requested_by=invocation.requested_by,
                target_kind=invocation.target_kind.value,
                target_id=target_id,
                target_name=name,
            ),
        )
        store.commit()
        return request


async def _ask(request: Request) -> Handoff | None:
    structured = model().with_structured_output(Handoff)
    return await structured.ainvoke(
        [SystemMessage(request.system), HumanMessage(request.chat)],
        {"callbacks": tracing.callbacks(), "run_name": "handoff", "metadata": request.metadata},
    )


def _record(data_store_id: uuid.UUID, invocation_id: int, handoff: Handoff) -> None:
    with store_session(data_store_id) as store:
        invocations.record_handoff(
            store, invocation_id, ack=handoff.ack.strip(), brief=handoff.brief.strip()
        )
        store.commit()


async def brief(data_store_id: uuid.UUID, invocation_id: int) -> None:
    """Run the store's queued invocation's handoff: it goes `briefing`, and its brief and ack
    are written. Nothing for one that has finished. Raises `CantStart` if the handoff fails."""
    request = await run_in_threadpool(_begin, data_store_id, invocation_id)
    if request is None:
        return
    try:
        handoff = await asyncio.wait_for(_ask(request), TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning("the handoff for invocation %s failed", invocation_id, exc_info=True)
        raise CantStart(f"The handoff failed: {str(exc) or type(exc).__name__}") from exc
    if handoff is None:
        raise CantStart("The handoff failed: the model gave no brief.")
    await run_in_threadpool(_record, data_store_id, invocation_id, handoff)
