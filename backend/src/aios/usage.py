"""The usage ledger: every billable call writes one row (`aios.datastores.models.UsageEntry`), in
the organisation's store, priced in pounds sterling from the price table (`price_table`) and
charged to its agent or workflow, run, turn, invocation, person and cause.

**What's charged to whom** is a `Charge`. A turn's (`for_turn`) is set as the current charge
(`charging`) while the turn's graph runs, so whatever the turn calls, its tools included, is
charged to it; the Search page sets the person's; the background embedding sweep passes its own.

**Recording points**, one for each kind of call:

- every model call of a turn, agent runs and workflow agent nodes alike: a middleware on every
  agent (`UsageMiddleware`, `aios.runner.build_agent`), which writes the row with the turn's
  counters in one transaction (`record_turn_call`), and also checks the caps first
  (`aios.limits`); the provider's web searches in the response are a row of their own;
- the handoff's call (`aios.handoff`), written with its brief;
- the embeddings module, once per batch (`aios.embeddings`);
- a sandbox session, when it ends (`sandbox_ended`): replaced by a new one, or stopped with its
  run;
- a post by `post_message`, as a tool call costing nothing, with the post
  (`aios.tools.post_message`).

A model call's row is keyed by its response's id (`call_key`): recording it again is a no-op, so
a retry never counts twice. A model with no price records a null cost, never an error.

Admins read totals by agent, workflow, person or day (`totals`, `aios.api.usage`); agents read
the rows through `reporting.usage`.
"""

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Any

from fastapi.concurrency import run_in_threadpool
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import limits as caps
from aios import streams, turns
from aios.config import get_settings
from aios.datastores import store_id_of, store_session
from aios.datastores.models import (
    Agent,
    AgentRun,
    Invocation,
    SandboxSession,
    Turn,
    UsageEntry,
    Workflow,
)
from aios.db import SessionLocal
from aios.models import (
    DataStore,
    InvocationCause,
    Organisation,
    UsageKind,
    UsagePurpose,
)

log = logging.getLogger(__name__)

# About how many characters a token is, for embeddings, whose providers don't report tokens.
CHARS_PER_TOKEN = 4
MILLION = 1_000_000


@dataclasses.dataclass(frozen=True)
class Charge:
    """What a billable call is charged to, and why it was made."""

    # The store the row is written in: the organisation's.
    data_store_id: uuid.UUID
    purpose: UsagePurpose
    agent_id: int | None = None
    workflow_id: int | None = None
    agent_run_id: int | None = None
    workflow_run_id: int | None = None
    turn_id: int | None = None
    invocation_id: int | None = None
    person_id: int | None = None
    cause: InvocationCause | None = None
    # The caps in force for a turn (`aios.limits`), by name; None checks none.
    limits: dict[str, float] | None = None

    def but(self, **changes: Any) -> "Charge":
        return dataclasses.replace(self, **changes)


_current: ContextVar[Charge | None] = ContextVar("usage_charge", default=None)


def current() -> Charge | None:
    """The charge in force here (`charging`), if any."""
    return _current.get()


@contextlib.contextmanager
def charging(charge: Charge | None) -> Iterator[None]:
    """Charge what's called within (and in tasks and threads started within) to `charge`."""
    token = _current.set(charge)
    try:
        yield
    finally:
        _current.reset(token)


# Prices.


def price(model: str | None, **quantities: float) -> float | None:
    """What these quantities of `model` cost in GBP, from the price table: per million tokens for
    `input`, `output`, `cache_read` and `cache_write`, per unit otherwise (`web_search`,
    `per_second`). None if the table has no price for the model, or for any quantity asked."""
    prices = get_settings().price_table.get(model or "")
    if prices is None:
        return None
    total = 0.0
    for name, quantity in quantities.items():
        if not quantity:
            continue
        if name not in prices:
            return None
        per = MILLION if name in ("input", "output", "cache_read", "cache_write") else 1
        total += prices[name] * quantity / per
    return total


# Writing rows.


def record(
    store: Session,
    charge: Charge,
    kind: UsageKind,
    *,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    quantity: float = 1,
    cost: float | None = None,
    call_key: str | None = None,
) -> bool:
    """Write one row (in the caller's transaction); False if one with `call_key` was already
    written."""
    values = {
        "kind": kind,
        "purpose": charge.purpose,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "quantity": quantity,
        "cost": cost,
        "agent_id": charge.agent_id,
        "workflow_id": charge.workflow_id,
        "agent_run_id": charge.agent_run_id,
        "workflow_run_id": charge.workflow_run_id,
        "turn_id": charge.turn_id,
        "invocation_id": charge.invocation_id,
        "person_id": charge.person_id,
        "cause": charge.cause,
        "call_key": call_key,
    }
    statement = (
        insert(UsageEntry)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[UsageEntry.call_key])
    )
    return store.scalar(statement.returning(UsageEntry.id)) is not None


def web_searches(response: AIMessage) -> int:
    """How many web searches the provider ran in the response: as its usage reports them, else
    its web search blocks."""
    usage = (response.response_metadata or {}).get("usage") or {}
    server = usage.get("server_tool_use") if isinstance(usage, dict) else None
    if isinstance(server, dict) and server.get("web_search_requests") is not None:
        return int(server["web_search_requests"])
    blocks = response.content if isinstance(response.content, list) else []
    return sum(
        isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "web_search"
        for b in blocks
    )


def tool_call_count(response: AIMessage) -> int:
    """The tool calls in a model response: the agent's, and the provider's own (e.g. web
    search), which are content blocks of the response (as the run page counts them)."""
    blocks = response.content if isinstance(response.content, list) else []
    server = sum(isinstance(b, dict) and b.get("type") == "server_tool_use" for b in blocks)
    return len(response.tool_calls) + server


def record_model_call(store: Session, charge: Charge, model: str, response: AIMessage) -> bool:
    """Write a model call's row, and its web searches' if it ran any (in the caller's
    transaction); False if it was recorded already."""
    usage = response.usage_metadata or {}
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read") or 0
    cache_write = details.get("cache_creation") or 0
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    key = response.id or f"call-{uuid.uuid4()}"
    # LangChain's input tokens include the cache's reads and writes.
    plain = max(0, input_tokens - cache_read - cache_write)
    written = record(
        store,
        charge,
        UsageKind.model_call,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost=price(
            model, input=plain, output=output_tokens, cache_read=cache_read, cache_write=cache_write
        ),
        call_key=f"model:{key}",
    )
    if written and (searches := web_searches(response)):
        record(
            store,
            charge,
            UsageKind.web_search,
            model=model,
            quantity=searches,
            cost=price(model, web_search=searches),
            call_key=f"web_search:{key}",
        )
    return written


def record_turn_call(charge: Charge, model: str, response: AIMessage) -> None:
    """A turn's model call: its rows and the turn's counters, in one transaction."""
    with store_session(charge.data_store_id) as store:
        if record_model_call(store, charge, model, response) and charge.turn_id is not None:
            usage = response.usage_metadata or {}
            turns.count(
                store,
                charge.turn_id,
                tool_calls=tool_call_count(response),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            streams.turn_changed(store, charge.turn_id)
        store.commit()


def record_embedding(
    charge: Charge | None, texts: Sequence[str], purpose: UsagePurpose | None = None
) -> None:
    """A batch of `texts` embedded, charged to `charge` (None: the current charge, if any; none
    at all records nothing), as `purpose` if given. Its tokens are estimated from the texts'
    length. Never raises: a row that can't be written is logged."""
    charge = charge or current()
    if charge is None or not texts:
        return
    if purpose is not None:
        charge = charge.but(purpose=purpose)
    model = get_settings().embedding_model
    tokens = sum(-(-len(t) // CHARS_PER_TOKEN) for t in texts)
    try:
        with store_session(charge.data_store_id) as store:
            record(
                store,
                charge,
                UsageKind.embedding,
                model=model,
                input_tokens=tokens,
                quantity=len(texts),
                cost=price(model, input=tokens),
            )
            store.commit()
    except Exception:
        log.exception("couldn't record the usage of an embedding batch")


SANDBOX = "sandbox"


def sandbox_ended(store: Session, session: SandboxSession, ended_at: datetime) -> None:
    """Write the seconds of the run's sandbox session (`session`, as it was), which ended by
    `ended_at` at the latest (in the caller's transaction): from its start to its last use plus
    the idle time it stops itself after, or its longest session, if either is sooner."""
    settings = get_settings()
    end = min(
        ended_at,
        session.last_used_at + timedelta(seconds=settings.sandbox_idle_seconds),
        session.created_at + timedelta(seconds=settings.sandbox_max_session_seconds),
    )
    seconds = max(0.0, (end - session.created_at).total_seconds())
    charge = for_run(store, session.agent_run_id)
    if charge is None:
        return
    record(
        store,
        charge,
        UsageKind.sandbox_seconds,
        model=SANDBOX,
        quantity=seconds,
        cost=price(SANDBOX, per_second=seconds),
    )


# What things are charged to.


def organisation_limits(data_store_id: uuid.UUID) -> dict:
    """The store's organisation's own caps (its overrides, `Organisation.limits`)."""
    with SessionLocal() as db:
        return (
            db.scalar(
                select(Organisation.limits)
                .join(DataStore, DataStore.owner_organisation_id == Organisation.id)
                .where(DataStore.id == data_store_id)
            )
            or {}
        )


def _invocation_charge(store: Session, invocation: Invocation | None) -> dict[str, Any]:
    """What an invocation charges: its cause and who asked, and the workflow run it's part of,
    for an agent node's (its parent's)."""
    if invocation is None:
        return {}
    values: dict[str, Any] = {
        "invocation_id": invocation.id,
        "cause": invocation.cause,
        "person_id": invocation.requested_by,
    }
    if invocation.workflow_run_id is not None:
        values |= {
            "workflow_run_id": invocation.workflow_run_id,
            "workflow_id": invocation.workflow_id,
        }
    elif invocation.parent_id is not None:
        parent = store.get(Invocation, invocation.parent_id)
        if parent is not None and parent.workflow_run_id is not None:
            values |= {"workflow_run_id": parent.workflow_run_id, "workflow_id": parent.workflow_id}
    return values


def for_turn(data_store_id: uuid.UUID, turn_id: int) -> Charge | None:
    """A turn's charge: its agent, run, invocation, the person who asked, the cause, the workflow
    run an agent node's turn is part of; and the caps in force for it. None if it's gone."""
    org_limits = organisation_limits(data_store_id)
    with store_session(data_store_id, "ro") as store:
        turn = store.get(Turn, turn_id)
        if turn is None:
            return None
        run = store.get_one(AgentRun, turn.agent_run_id)
        invocation = store.get(Invocation, turn.invocation_id) if turn.invocation_id else None
        values = {"person_id": run.created_by} | _invocation_charge(store, invocation)
        if values.get("person_id") is None:
            values["person_id"] = run.created_by
        agent_limits = store.scalar(select(Agent.limits).where(Agent.id == run.agent_id))
        workflow_limits = None
        if values.get("workflow_id") is not None:
            workflow_limits = store.scalar(
                select(Workflow.limits).where(Workflow.id == values["workflow_id"])
            )
        return Charge(
            data_store_id=data_store_id,
            purpose=UsagePurpose.turn,
            agent_id=run.agent_id,
            agent_run_id=run.id,
            turn_id=turn.id,
            limits=caps.in_force(org_limits, agent_limits, workflow_limits),
            **values,
        )


def for_run(store: Session, run_id: int) -> Charge | None:
    """An agent run's charge, as of its latest turn (or its invocation, before it has one), on
    a session on its store; None if it's gone."""
    run = store.get(AgentRun, run_id)
    if run is None:
        return None
    turn_id = store.scalar(
        select(Turn.id).where(Turn.agent_run_id == run_id).order_by(Turn.id.desc()).limit(1)
    )
    invocation = store.scalars(
        select(Invocation).where(Invocation.agent_run_id == run_id).order_by(Invocation.id)
    ).first()
    values = _invocation_charge(store, invocation)
    if values.get("person_id") is None:
        values["person_id"] = run.created_by
    return Charge(
        data_store_id=store_id_of(store),
        purpose=UsagePurpose.turn,
        agent_id=run.agent_id,
        agent_run_id=run.id,
        turn_id=turn_id,
        **values,
    )


# Caps.


class LimitReached(Exception):
    """A turn stopped before a model call that would pass one of its caps (`aios.limits`)."""


def check_caps(charge: Charge) -> None:
    """Raise `LimitReached` if the turn (or its workflow run) is at one of its caps."""
    limits = charge.limits
    if not limits or charge.turn_id is None:
        return
    with store_session(charge.data_store_id, "ro") as store:
        turn = store.execute(
            select(Turn.model_calls, Turn.input_tokens + Turn.output_tokens).where(
                Turn.id == charge.turn_id
            )
        ).one_or_none()
        if turn is None:
            return
        cost = store.scalar(
            select(func.coalesce(func.sum(UsageEntry.cost), 0)).where(
                UsageEntry.turn_id == charge.turn_id
            )
        )
        used = {"turn_model_calls": turn[0], "turn_tokens": turn[1], "turn_cost": cost}
        if charge.workflow_run_id is not None:
            calls, run_cost = store.execute(
                select(
                    func.count().filter(UsageEntry.kind == UsageKind.model_call),
                    func.coalesce(func.sum(UsageEntry.cost), 0),
                ).where(UsageEntry.workflow_run_id == charge.workflow_run_id)
            ).one()
            used |= {"run_model_calls": calls, "run_cost": run_cost}
    for name, value in used.items():
        if name in limits and value >= limits[name]:
            raise LimitReached(caps.reached(name, limits[name]))


def recursion_limit(charge: Charge | None) -> int | None:
    """LangGraph's recursion limit for a turn, from its model-call cap: room for each call and
    its tools, so the cap, not the recursion limit, is what stops it."""
    if charge is None or not charge.limits or "turn_model_calls" not in charge.limits:
        return None
    return int(charge.limits["turn_model_calls"]) * 2 + 10


class UsageMiddleware(AgentMiddleware):
    """On every agent (`aios.runner.build_agent`): before each model call of a turn, its caps
    (`check_caps`); after it, its usage row and the turn's counters (`record_turn_call`). Only
    under a turn's charge (`charging`); a row that can't be written doesn't end the turn."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        charge = current()
        if charge is None or charge.turn_id is None:
            return await handler(request)
        try:
            await run_in_threadpool(check_caps, charge)
        except LimitReached:
            raise
        except Exception:
            # The caps can't be read just now (the store busy, say): the call goes ahead.
            log.exception("couldn't check the caps of turn %s", charge.turn_id)
        response = await handler(request)
        for message in response.result:
            if isinstance(message, AIMessage):
                try:
                    await run_in_threadpool(record_turn_call, charge, self.model, message)
                except Exception:
                    log.exception("couldn't record a model call of turn %s", charge.turn_id)
        return response


# Reading totals.

GROUPS = ("agent", "workflow", "person", "day")


def totals(store: Session, group: str, start: date, end: date) -> list[dict[str, Any]]:
    """The ledger's totals from `start` to `end` (days, inclusive, UTC) by `group`: agent,
    workflow, person or day. Each: the key (an id, or the day), model calls, tokens in and out,
    embeddings' texts, web searches, tool calls, sandbox seconds, the cost (null if nothing in
    it was priced) and how many entries are unpriced."""
    key = {
        "agent": UsageEntry.agent_id,
        "workflow": UsageEntry.workflow_id,
        "person": UsageEntry.person_id,
        "day": cast(func.timezone("UTC", UsageEntry.at), Date),
    }[group]

    def quantity(kind: UsageKind):
        return func.coalesce(func.sum(UsageEntry.quantity).filter(UsageEntry.kind == kind), 0)

    rows = store.execute(
        select(
            key.label("key"),
            func.count().filter(UsageEntry.kind == UsageKind.model_call).label("model_calls"),
            func.coalesce(func.sum(UsageEntry.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageEntry.output_tokens), 0).label("output_tokens"),
            quantity(UsageKind.embedding).label("embedded_texts"),
            quantity(UsageKind.web_search).label("web_searches"),
            quantity(UsageKind.tool_call).label("tool_calls"),
            quantity(UsageKind.sandbox_seconds).label("sandbox_seconds"),
            func.sum(UsageEntry.cost).label("cost"),
            func.count().filter(UsageEntry.cost.is_(None)).label("unpriced"),
        )
        .where(
            UsageEntry.at >= func.timezone("UTC", cast(start, Date)),
            UsageEntry.at < func.timezone("UTC", cast(end + timedelta(days=1), Date)),
        )
        .group_by(key)
        .order_by(key.nulls_last())
    ).all()
    return [dict(row._mapping) for row in rows]
