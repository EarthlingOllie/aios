"""Prompted charts and saved charts, on the Insights page (`aios.api.insights`).

**Reading.** A chart's one SQL statement runs as the store's insights role
(`<prefix>_insights`, `aios.datastores.provision`), which reads only the `insights` views
(metrics, no content) and the organisation's `data` tables: so "metrics only" is the
database's guarantee, whatever the SQL says. It goes through the data tools' read-only path
(`aios.datastores.reads`): one statement, in a `READ ONLY` transaction that's rolled back,
under `chart_statement_timeout`, with the connection reset after; its rows are capped at
`chart_max_rows` and `chart_max_bytes`, and a result cut short is drawn and marked truncated.

**Specs** (`check_spec`) are Vega-Lite, checked against its schema (the pinned version's,
`schemas/`, the same the web app draws with), then an allowlist:

- marks: bar, line, area, point, rect, arc, tick, rule and text;
- transforms: aggregate, bin, time unit, fold, stack and window (with their sorts);
- refused anywhere: data of its own (aios always supplies the query's rows inline), links and
  URL encodings, images, signal parameters and expression strings.

So a chart can't fetch from, or send data to, anywhere.

**Asking** (`ask`). A member's question is an invocation (cause `insight`) of the built-in
insights agent (`insights_agent`: an ordinary agent of the organisation's, found by `builtin`,
made the first time someone asks), charged to whoever asked: the API rate-limits it per person
(`chart_questions_per_hour`) and refuses it at the budget or the asker's allowance
(`aios.budgets`) before any model call. The agent is sent its instructions, what the insights
role may read (`catalogue`) and the question, and answers with a title, one SQL statement and a
spec (`ChartAnswer`). A spec that fails its checks, or SQL that fails to run, goes back to the
agent once with the reason; failing again, the error is shown with the SQL. Each model call is
a usage ledger entry (purpose `insight`) against the asker.

**Saved charts** (`aios.datastores.models.InsightChart`) belong to the organisation: viewing
one re-runs its SQL (never the agent) through `run_query`, cached as the panels are
(`aios.insights.cached`).
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

import jsonschema
import psycopg
from fastapi.concurrency import run_in_threadpool
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios import llm, model_rate_limit, tracing, usage
from aios.api.provenance import built_by
from aios.config import get_settings
from aios.datastores import reads, store_session
from aios.datastores.models import Agent, Invocation
from aios.models import (
    InvocationCause,
    InvocationStatus,
    TriggerTargetKind,
    UsagePurpose,
)
from aios.versioning import save_agent_version

log = logging.getLogger(__name__)


class ChartQueryError(Exception):
    """A chart's query didn't run: the database's reason."""


def run_query(data_store_id: uuid.UUID, sql: str) -> reads.CappedRows:
    """Run a chart's SQL as the store's insights role, through the capped read-only path.
    Raises `ChartQueryError` with the database's reason if it doesn't run."""
    settings = get_settings()
    try:
        with reads.read_only(data_store_id, "insights") as pg:
            timeout = min(settings.chart_statement_timeout, settings.data_store_statement_timeout)
            pg.execute(f"SET LOCAL statement_timeout = {int(timeout)}")
            return reads.read_capped(
                pg,
                sql,
                max_rows=settings.chart_max_rows,
                max_bytes=settings.chart_max_bytes,
                ensure_ascii=False,
            )
    except psycopg.Error as exc:
        raise ChartQueryError(_reason(exc)) from exc


def _reason(exc: psycopg.Error) -> str:
    diag = getattr(exc, "diag", None)
    message = (diag.message_primary if diag else None) or str(exc).strip()
    return message.splitlines()[0] if message else type(exc).__name__


# --- Specs -------------------------------------------------------------------------------------

VEGA_LITE_VERSION = "6.5.0"
MARKS = frozenset({"bar", "line", "area", "point", "rect", "arc", "tick", "rule", "text"})
TRANSFORMS = frozenset({"aggregate", "bin", "timeUnit", "fold", "stack", "window"})
# Every kind of Vega-Lite transform, by the key that names it: those not in `TRANSFORMS` are
# refused by name (`calculate` and `filter` take expression strings).
ALL_TRANSFORMS = TRANSFORMS | frozenset(
    {
        "calculate",
        "density",
        "extent",
        "filter",
        "flatten",
        "impute",
        "joinaggregate",
        "loess",
        "lookup",
        "pivot",
        "quantile",
        "regression",
        "sample",
    }
)
# The most a spec may be, as JSON.
MAX_SPEC_BYTES = 64 * 1024

EXPRESSIONS = "Expression strings aren't allowed in charts"


@cache
def _validator() -> jsonschema.Draft7Validator:
    text = (
        resources.files("aios")
        .joinpath(f"schemas/vega-lite-v{VEGA_LITE_VERSION}.json")
        .read_text(encoding="utf-8")
    )
    return jsonschema.Draft7Validator(json.loads(text))


def _where(path: list) -> str:
    return "/".join(str(p) for p in path) or "the top level"


def _refusal(node: Any, path: list) -> str | None:
    """What's refused in `node` (at `path` in the spec), if anything: the allowlist."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            if problem := _refusal(item, [*path, i]):
                return problem
        return None
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        here = [*path, key]
        if key in ("data", "datasets") and path:
            return f"A chart can't bring data of its own ({_where(here)}): its data is its query's."
        if key == "datasets":
            return "A chart can't bring data of its own: its data is its query's."
        if key == "params":
            return f"Signal parameters aren't allowed in charts ({_where(here)})."
        if key in ("expr", "signal", "test") or key.endswith("Expr"):
            return f"{EXPRESSIONS} ({_where(here)})."
        if key == "href":
            return f"Links aren't allowed in charts ({_where(here)})."
        if key == "url":
            return f"URL encodings and URLs aren't allowed in charts ({_where(here)})."
        if key == "mark":
            kind = value.get("type") if isinstance(value, dict) else value
            if kind == "image":
                return f"Images aren't allowed in charts ({_where(here)})."
            if kind not in MARKS:
                allowed = ", ".join(sorted(MARKS))
                return f"The mark {kind!r} isn't allowed in charts; use one of {allowed}."
        if key == "transform" and isinstance(value, list):
            for i, transform in enumerate(value):
                kinds = (
                    [k for k in transform if k in ALL_TRANSFORMS]
                    if isinstance(transform, dict)
                    else []
                )
                if (
                    kinds
                    and isinstance(transform[kinds[0]], str)
                    and kinds[0]
                    in (
                        "calculate",
                        "filter",
                    )
                ):
                    return f"{EXPRESSIONS} ({_where([*here, i, kinds[0]])})."
                if not kinds or kinds[0] not in TRANSFORMS:
                    named = kinds[0] if kinds else "unknown"
                    allowed = ", ".join(sorted(TRANSFORMS))
                    return f"The {named} transform isn't allowed in charts; use {allowed}."
        if problem := _refusal(value, here):
            return problem
    return None


def check_spec(spec: Any) -> str | None:
    """Why the Vega-Lite spec (without its data, which is the query's) can't be drawn, if it
    can't: too big, not Vega-Lite (its schema), or a feature the allowlist refuses."""
    if not isinstance(spec, dict):
        return "The chart's spec isn't a Vega-Lite spec (a JSON object)."
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        return f"The chart's spec is over {MAX_SPEC_BYTES // 1024} KiB."
    top = spec.get("data")
    if isinstance(top, dict) and ("url" in top or "values" in top or "sequence" in top):
        return "A chart can't bring data of its own: its data is its query's."
    if problem := _refusal({k: v for k, v in spec.items() if k != "data"}, []):
        return problem
    drawn = {**_without_schema(spec), "data": {"values": []}}
    error = jsonschema.exceptions.best_match(_validator().iter_errors(drawn))
    if error is not None:
        where = _where(list(error.absolute_path))
        return f"The chart's spec isn't valid Vega-Lite ({where}): {error.message[:300]}"
    return None


def _without_schema(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if k != "$schema"}


def drawable(spec: dict) -> dict:
    """The spec as the app draws it: without data of its own or a `$schema` (the app's pinned
    Vega-Lite draws it)."""
    return {k: v for k, v in spec.items() if k not in ("$schema", "data")}


# --- The insights agent ------------------------------------------------------------------------

BUILTIN = "insights"
NAME = "Insights agent"
DESCRIPTION = (
    "Draws charts of the organisation's metrics when asked on the Insights page. Built in."
)
# The transaction-level advisory lock making it takes, in the organisation's store.
LOCK = 0x61696F73_63687274  # "aioschrt"
INSTRUCTIONS = f"""\
You draw charts of an organisation's metrics. Given a question, answer with:

- title: a short title for the chart;
- sql: exactly one PostgreSQL SELECT statement (no semicolons, no other statement) that returns \
the rows to draw, reading only the relations listed below, always schema-qualified;
- spec: a Vega-Lite (v6) spec drawing those rows by their column names, without "data" (the \
rows are supplied inline) and without "$schema".

The spec may use only these marks: {", ".join(sorted(MARKS))}; and only these transforms: \
{", ".join(sorted(TRANSFORMS))}. Never use params (signal parameters), expressions \
("expr", "calculate", "filter", "test", or any "...Expr" property), links ("href"), URLs or \
images. Prefer doing the work in SQL. Keep results small: aggregate rather than list rows."""

# The most characters of a question.
MAX_QUESTION = 2000
# The most lines of the catalogue the agent is sent.
CATALOGUE_LINES = 600


def _free_name(store: Session) -> str:
    taken = set(store.scalars(select(Agent.name).where(Agent.name.startswith(NAME))))
    n = 1
    while (name := NAME if n == 1 else f"{NAME} {n}") in taken:
        n += 1
    return name


def insights_agent(store: Session, user_id: int) -> Agent:
    """The organisation's insights agent, in its store, made by `user_id` (whoever first asks)
    if it has none (archived or not); in the caller's transaction, which holds a lock so two
    first questions at once share one."""
    store.execute(select(func.pg_advisory_xact_lock(LOCK)))
    agent = store.scalars(select(Agent).where(Agent.builtin == BUILTIN)).one_or_none()
    if agent is not None:
        return agent
    agent = Agent(**built_by(user_id), name=_free_name(store), description=DESCRIPTION)
    agent.builtin = BUILTIN
    store.add(agent)
    save_agent_version(
        store,
        agent,
        base_prompt=INSTRUCTIONS,
        skills=[],
        tools=[],
        note=None,
        user_id=user_id,
    )
    return agent


def catalogue(data_store_id: uuid.UUID) -> str:
    """What the insights role may read, as the agent is told it: each relation, what it is, and
    its columns with their types; read as the role itself, so it's exactly that."""
    with reads.read_only(data_store_id, "insights") as pg:
        columns = pg.execute(
            "SELECT c.table_schema, c.table_name, c.column_name, c.data_type,"
            " obj_description(format('%I.%I', c.table_schema, c.table_name)::regclass)"
            " FROM information_schema.columns c"
            " WHERE c.table_schema IN ('insights', 'data')"
            " ORDER BY c.table_schema DESC, c.table_name, c.ordinal_position"
        ).fetchall()
    lines: list[str] = []
    current = None
    for schema, table, column, kind, comment in columns:
        if (schema, table) != current:
            current = (schema, table)
            lines.append(f"{schema}.{table}" + (f": {comment}" if comment else ""))
        lines.append(f"  {column} {kind}")
    if len(lines) > CATALOGUE_LINES:
        lines = [*lines[:CATALOGUE_LINES], "  (more not shown)"]
    return "\n".join(lines) or "(nothing)"


class ChartAnswer(BaseModel):
    """A chart answering the question."""

    title: str = Field(description="A short title for the chart.")
    sql: str = Field(description="Exactly one PostgreSQL SELECT statement returning the rows.")
    spec: dict[str, Any] = Field(
        description="A Vega-Lite spec drawing the rows by column name, without data or $schema."
    )


@dataclass
class Asked:
    """What asking gave: the chart and its rows, or an error with the SQL (if any)."""

    question: str
    title: str | None = None
    sql: str | None = None
    spec: dict | None = None
    columns: list[str] | None = None
    data: list[dict] | None = None
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _Question:
    invocation_id: int
    agent_id: int
    system: str
    charge: usage.Charge
    metadata: dict[str, Any]


def _begin(
    data_store_id: uuid.UUID, organisation_id: int, user_id: int, question: str
) -> _Question:
    """The asker's invocation of the insights agent (made if need be), running, and what the
    agent is sent."""
    with store_session(data_store_id) as store:
        agent = insights_agent(store, user_id)
        store.flush()
        invocation = Invocation(
            cause=InvocationCause.insight,
            target_kind=TriggerTargetKind.agent,
            agent_id=agent.id,
            requested_by=user_id,
            charged_to=user_id,
            status=InvocationStatus.running,
            started_at=func.now(),
        )
        store.add(invocation)
        store.flush()
        version = agent.current_version
        instructions = version.base_prompt if version is not None else INSTRUCTIONS
        asked = _Question(
            invocation_id=invocation.id,
            agent_id=agent.id,
            system=instructions,
            charge=usage.Charge(
                data_store_id,
                UsagePurpose.insight,
                agent_id=agent.id,
                invocation_id=invocation.id,
                person_id=user_id,
                cause=InvocationCause.insight,
            ),
            metadata=tracing.trace_metadata(
                session_id=None,
                organisation_id=organisation_id,
                created_by=user_id,
                tags=["insights"],
                invocation_id=invocation.id,
                agent_id=agent.id,
            ),
        )
        store.commit()
    relations = catalogue(data_store_id)
    system = f"{asked.system}\n\nWhat you may read:\n{relations}"
    return _Question(asked.invocation_id, asked.agent_id, system, asked.charge, asked.metadata)


def model_name() -> str:
    return llm.model_name(get_settings().insights_model or None)


async def _call(
    question: _Question, messages: list[BaseMessage]
) -> tuple[ChartAnswer | None, AIMessage | None]:
    structured = llm.chat_model(get_settings().insights_model or None).with_structured_output(
        ChartAnswer, include_raw=True
    )
    name = model_name()
    await model_rate_limit.acquire(name)
    answer = await structured.ainvoke(
        messages,
        {"callbacks": tracing.callbacks(), "run_name": "insights", "metadata": question.metadata},
    )
    raw = answer.get("raw")
    raw = raw if isinstance(raw, AIMessage) else None
    await model_rate_limit.spend(name, model_rate_limit.tokens_of(raw))
    return answer.get("parsed"), raw


def _record(question: _Question, response: AIMessage | None) -> None:
    if response is None:
        return
    with store_session(question.charge.data_store_id) as store:
        usage.record_model_call(store, question.charge, model_name(), response)
        store.commit()


def _finish(data_store_id: uuid.UUID, invocation_id: int, error: str | None) -> None:
    with store_session(data_store_id) as store:
        invocation = store.get(Invocation, invocation_id)
        if invocation is not None:
            invocation.status = InvocationStatus.failed if error else InvocationStatus.succeeded
            invocation.error = error
            invocation.finished_at = func.now()
        store.commit()


def records(rows: reads.CappedRows) -> list[dict]:
    """Rows as Vega-Lite's inline data: one object per row, by column name."""
    return [dict(zip(rows.columns, row, strict=True)) for row in rows.rows]


def _attempt(data_store_id: uuid.UUID, answer: ChartAnswer | None) -> tuple[Asked, str | None]:
    """The answer checked and its SQL run: what to show, and why it failed (if it did)."""
    if answer is None:
        return Asked(question=""), "The agent gave no chart."
    asked = Asked(question="", title=answer.title.strip()[:200], sql=answer.sql, spec=answer.spec)
    if problem := check_spec(answer.spec):
        return asked, problem
    asked.spec = drawable(answer.spec)
    try:
        rows = run_query(data_store_id, answer.sql)
    except ChartQueryError as exc:
        return asked, f"The query failed: {exc}"
    asked.columns, asked.data, asked.truncated = rows.columns, records(rows), rows.truncated
    return asked, None


def _fix_request(answer: ChartAnswer | None, problem: str) -> HumanMessage:
    previous = (
        json.dumps({"sql": answer.sql, "spec": answer.spec}) if answer is not None else "(none)"
    )
    return HumanMessage(
        f"Your chart can't be drawn: {problem}\n\nYour answer was: {previous}\n\n"
        "Answer again with a corrected chart."
    )


class AgentFailed(RuntimeError):
    """The insights agent's model call failed: no chart, and nothing to show of one."""


async def ask(data_store_id: uuid.UUID, organisation_id: int, user_id: int, question: str) -> Asked:
    """Ask the insights agent `question` for the member `user_id` (see the module docstring):
    after the API's rate and budget checks. Raises `AgentFailed` if its model can't be called."""
    begun = await run_in_threadpool(_begin, data_store_id, organisation_id, user_id, question)
    messages: list[BaseMessage] = [SystemMessage(begun.system), HumanMessage(question)]
    timeout = get_settings().chart_model_timeout_seconds
    asked, problem = Asked(question=question), None
    try:
        for attempt in range(2):
            try:
                answer, raw = await asyncio.wait_for(_call(begun, messages), timeout)
            except Exception as exc:
                log.warning("the insights agent failed", exc_info=True)
                raise AgentFailed(
                    f"The insights agent couldn't answer: {str(exc) or type(exc).__name__}"
                ) from exc
            await run_in_threadpool(_record, begun, raw)
            asked, problem = await run_in_threadpool(_attempt, data_store_id, answer)
            if problem is None or attempt == 1:
                break
            # The previous answer as text, not its tool call (which a provider wants answered
            # with a tool result).
            messages.append(_fix_request(answer, problem))
    except AgentFailed as exc:
        await run_in_threadpool(_finish, data_store_id, begun.invocation_id, str(exc))
        raise
    asked.question = question
    if problem is not None:
        asked.error, asked.data, asked.columns = problem, None, None
    await run_in_threadpool(_finish, data_store_id, begun.invocation_id, problem)
    return asked
