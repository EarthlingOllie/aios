"""The turns of agent runs, as records (`aios.datastores.models.Turn`).

A turn begins when a run is given a message (or carries on after a schema change request is
decided) and ends with the agent's reply, or why it failed. Each records the invocation its run
ran for, the agent version it ran on, how it went, when it began and ended, and its model calls,
tool calls and tokens (`count`, once per model call).

A run's status, times and counters are its turns' (`run_columns`): its status and times are its
latest turn's, its counters add up over all of them. A run with no turns yet has none of them.

Every turn runs under its run's turn lease (`aios.turn_lease`), so a run's turns don't overlap,
but each is still written by its own id: a turn that couldn't take the lease ends without
touching the one that held it.

Everything here is on a session on the run's store, in the caller's transaction.
"""

from collections import Counter
from collections.abc import Iterable, Sequence

from sqlalchemy import Select, func, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import invocations, streams
from aios.datastores.models import AgentRun, Invocation, Turn, TurnToolCall
from aios.models import AgentRunStatus, InvocationStatus


def begin(
    store: Session,
    run_id: int,
    invocation_id: int | None = None,
    *,
    error: str | None = None,
    version_id: int | None = None,
) -> int | None:
    """Begin a turn of the store's run, on the run's version (its latest turn's; a conversation
    run's caller has moved it to the agent's current version first), for `invocation_id` (which
    is running from now, if it was waiting to): by default the run's invocation that's running
    (a conversation run's turn carrying on after an approval), else its first. Its id, or None
    if the run is gone.

    With `error`, the turn ends as it begins, failed with why, on `version_id`: a conversation
    run's turn whose current version can't run, which leaves the run on its last version."""
    run = store.execute(
        select(AgentRun.id, AgentRun.agent_version_id).where(AgentRun.id == run_id)
    ).one_or_none()
    if run is None:
        return None
    if invocation_id is None:
        invocation_id = store.scalar(
            select(Invocation.id)
            .where(Invocation.agent_run_id == run_id)
            .order_by((Invocation.status == InvocationStatus.running).desc(), Invocation.id)
            .limit(1)
        )
    turn = Turn(
        agent_run_id=run_id,
        invocation_id=invocation_id,
        agent_version_id=version_id or run.agent_version_id,
        status=AgentRunStatus.running if error is None else AgentRunStatus.failed,
        error=error,
        finished_at=None if error is None else func.now(),
    )
    store.add(turn)
    if invocation_id is not None:
        invocations.mark_running(store, invocation_id)
    store.flush()
    return turn.id


def note(store: Session, turn_id: int, notices: list[str]) -> None:
    """Note on the turn what its run was told about tools left out of it (`aios.tools`'
    notices: a connector that's unavailable, a tool that vanished upstream)."""
    if notices:
        store.execute(update(Turn).where(Turn.id == turn_id).values(notices=list(notices)))


def count(
    store: Session,
    turn_id: int,
    *,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
    tools: Iterable[str] = (),
):
    """Add a model call to the turn's counters, and its tool calls by name (`tools`) to the
    turn's calls of each tool."""
    for tool, calls in Counter(tools).items():
        statement = insert(TurnToolCall).values(turn_id=turn_id, tool=tool[:255], calls=calls)
        store.execute(
            statement.on_conflict_do_update(
                index_elements=[TurnToolCall.turn_id, TurnToolCall.tool],
                set_={"calls": TurnToolCall.calls + statement.excluded.calls},
            )
        )
    store.execute(
        update(Turn)
        .where(Turn.id == turn_id)
        .values(
            model_calls=Turn.model_calls + 1,
            tool_calls=Turn.tool_calls + tool_calls,
            input_tokens=Turn.input_tokens + input_tokens,
            output_tokens=Turn.output_tokens + output_tokens,
        )
    )


def end(store: Session, turn_id: int, error: str | None) -> bool:
    """End the running turn: succeeded, or failed with `error`. Whether it was still running (a
    turn failed as stalled stays failed). Its run's card is published (`aios.streams`): a turn
    that ends waiting on an approval finishes no invocation and posts no message, and its card
    says so only by this."""
    ended = store.execute(
        update(Turn)
        .where(Turn.id == turn_id, Turn.status == AgentRunStatus.running)
        .values(
            status=AgentRunStatus.failed if error is not None else AgentRunStatus.succeeded,
            error=error,
            finished_at=func.now(),
        )
    )
    if ended.rowcount:
        streams.turn_changed(store, turn_id)
    return bool(ended.rowcount)


def fail_running(store: Session, *where, error: str) -> list[int]:
    """Fail the running turns of the runs matching `where` (conditions on `AgentRun`), with
    `error`; the ids of the runs whose turns were failed."""
    failed = store.execute(
        update(Turn)
        .where(
            Turn.status == AgentRunStatus.running,
            Turn.agent_run_id.in_(select(AgentRun.id).where(*where)),
        )
        .values(status=AgentRunStatus.failed, error=error, finished_at=func.now())
        .returning(Turn.agent_run_id)
    ).scalars()
    return sorted(set(failed))


def running(run_id):
    """Whether the agent run (an id, or a column of one) has a turn running."""
    return (
        select(Turn.id)
        .where(Turn.agent_run_id == run_id, Turn.status == AgentRunStatus.running)
        .exists()
    )


def run_columns(query: Select) -> Select:
    """`query` on `AgentRun` with its run's derived columns: `status`, `started_at` and
    `finished_at` (its latest turn's) and `tool_call_count`, `input_tokens` and `output_tokens`
    (summed over its turns); all null for a run with no turns."""
    latest = (
        select(Turn.status, Turn.started_at, Turn.finished_at)
        .where(Turn.agent_run_id == AgentRun.id)
        .order_by(Turn.id.desc())
        .limit(1)
        .lateral("latest_turn")
    )
    totals = (
        select(
            func.sum(Turn.tool_calls).label("tool_calls"),
            func.sum(Turn.input_tokens).label("input_tokens"),
            func.sum(Turn.output_tokens).label("output_tokens"),
        )
        .where(Turn.agent_run_id == AgentRun.id)
        .lateral("turn_totals")
    )
    return (
        query.add_columns(
            latest.c.status,
            latest.c.started_at,
            latest.c.finished_at,
            totals.c.tool_calls.label("tool_call_count"),
            totals.c.input_tokens,
            totals.c.output_tokens,
        )
        .outerjoin_from(AgentRun, latest, true())
        .outerjoin_from(AgentRun, totals, true())
    )


def invocation_columns(invocation_ids: Sequence[int], *where) -> Select:
    """For each of these invocations, its turns of the agent runs matching `where` (conditions
    on `AgentRun`), as `run_columns` gives a run's: `invocation_id` with `status`, `started_at`
    and `finished_at` (its latest turn's) and `tool_call_count`, `input_tokens` and
    `output_tokens` (summed over its turns). A conversation run has turns for many invocations;
    this is one mention's part of it. An invocation with no turns has no row."""
    ranked = (
        select(
            Turn.invocation_id,
            Turn.status,
            Turn.started_at,
            Turn.finished_at,
            func.row_number()
            .over(partition_by=Turn.invocation_id, order_by=Turn.id.desc())
            .label("rank"),
            func.sum(Turn.tool_calls).over(partition_by=Turn.invocation_id).label("tool_calls"),
            func.sum(Turn.input_tokens).over(partition_by=Turn.invocation_id).label("input"),
            func.sum(Turn.output_tokens).over(partition_by=Turn.invocation_id).label("output"),
        )
        .join(AgentRun, AgentRun.id == Turn.agent_run_id)
        .where(Turn.invocation_id.in_(list(invocation_ids)), *where)
        .subquery()
    )
    return select(
        ranked.c.invocation_id,
        ranked.c.status,
        ranked.c.started_at,
        ranked.c.finished_at,
        ranked.c.tool_calls.label("tool_call_count"),
        ranked.c.input.label("input_tokens"),
        ranked.c.output.label("output_tokens"),
    ).where(ranked.c.rank == 1)


def of_run(store: Session, run_id: int) -> list[Turn]:
    """The store's run's turns, oldest first."""
    return list(store.scalars(select(Turn).where(Turn.agent_run_id == run_id).order_by(Turn.id)))
