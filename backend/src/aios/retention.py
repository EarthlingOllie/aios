"""Retention: how long an organisation keeps its runs and its chat history, and the nightly job
that prunes what's older (`prune_all`, on the routine worker's beat at
`retention_prune_hour`).

Two settings on the organisation (`Organisation.run_retention_days`, `.chat_retention_days`),
from 1 to 36,500 days; null, the default, keeps everything indefinitely. Only admins set them
(`aios.api.organisation`).

- **Runs** go once finished for longer than the setting: an agent run with its turns, sandbox
  session (stopped too), checkpoint thread and search entries; a workflow run with its steps and
  its agent nodes' runs (and theirs). Never pruned: a run whose turn is running, that a turn
  holds, whose invocation is unfinished, or that waits on a schema change request; a workflow
  run queued, running or waiting. An agent node's run goes with its workflow run, not alone.
  A run used again lately isn't pruned whole: a conversation run's (`chat_id` set) oldest
  turns go turn by turn instead (`_prune_turns`), each with its messages in the run's history
  and its search entries, while the run is idle (no turn running or holding it, no invocation
  unfinished, not waiting on a schema change request). The cut is only made before a person's
  message, so a tool call is never parted from its result. A compaction summary
  (`aios.compaction`) goes once every turn it covers is gone.
- **Messages** go once older than the setting, with their mentions (on the message), the
  invocations they started (with those invocations' cards, results, briefs and search entries)
  and their own search entries; their chat stays. Never pruned: a message whose invocation, or
  one it started, is unfinished, or one whose invocation finished after the cutoff (its result
  is newer).
- **Never touched:** documents, data tables, memory, usage and the audit log.

Each kind with a setting is pruned `BATCH` rows at a time, each batch its own transaction, so
no lock is held long; then one audit entry per kind records how many went and the cutoff.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage
from sqlalchemy import ColumnElement, and_, delete, exists, false, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from aios import audit, checkpoints
from aios.datastores import store_session
from aios.datastores.models import (
    AgentRun,
    ChatMessage,
    Invocation,
    RunSummary,
    SchemaChangeRequest,
    SearchItem,
    Turn,
    WorkflowRun,
    WorkflowRunStep,
)
from aios.datastores.schema_changes.expiry import not_given
from aios.db import SessionLocal
from aios.invocations import LIVE
from aios.models import (
    AgentRunStatus,
    DataStore,
    DataStoreStatus,
    Organisation,
    WorkflowRunStatus,
)
from aios.sandbox.sessions import SandboxHandle, run_sandboxes, stop_sandboxes

log = logging.getLogger(__name__)

# Rows deleted per transaction.
BATCH = 1000

FINISHED_WORKFLOW_RUN = (WorkflowRunStatus.succeeded, WorkflowRunStatus.failed)


@dataclass(frozen=True)
class Settings:
    """An organisation's retention, in days; None keeps that kind indefinitely."""

    run_days: int | None
    chat_days: int | None


@dataclass(frozen=True)
class Pruned:
    """What one prune of a store took: None for a kind kept indefinitely. `turns` are those
    of conversation runs kept (with the runs' setting)."""

    runs: int | None
    messages: int | None
    turns: int | None = None


def prune_all(now: datetime | None = None) -> dict[uuid.UUID, Pruned | BaseException]:
    """Prune every ready store whose organisation has a setting (`prune`), one at a time;
    returns what each pruned, or what stopped it, without stopping the rest."""
    from celery.exceptions import SoftTimeLimitExceeded

    with SessionLocal() as db:
        stores = db.execute(
            select(DataStore.id, Organisation.run_retention_days, Organisation.chat_retention_days)
            .join(Organisation, Organisation.id == DataStore.owner_organisation_id)
            .where(
                DataStore.status == DataStoreStatus.ready,
                or_(
                    Organisation.run_retention_days.is_not(None),
                    Organisation.chat_retention_days.is_not(None),
                ),
            )
            .order_by(DataStore.created_at)
        ).all()
    outcome: dict[uuid.UUID, Pruned | BaseException] = {}
    for store_id, run_days, chat_days in stores:
        try:
            outcome[store_id] = prune(store_id, Settings(run_days, chat_days), now)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - one store's failure is reported, not raised
            log.exception("Pruning data store %s failed", store_id)
            outcome[store_id] = exc
    return outcome


def prune(store_id: uuid.UUID, settings: Settings, now: datetime | None = None) -> Pruned:
    """Prune the store's runs and messages older than `settings` say (as of `now`), recording
    one audit entry per kind with a setting."""
    now = now or datetime.now(UTC)
    runs = messages = turns = None
    if settings.run_days is not None:
        cutoff = now - timedelta(days=settings.run_days)
        runs = _in_batches(store_id, lambda s: _prune_runs(s, cutoff))
        _record(store_id, "runs.pruned", runs, cutoff)
        after = [0]
        turns = _in_batches(store_id, lambda s: _prune_turns(s, cutoff, after))
        _record(store_id, "turns.pruned", turns, cutoff)
    if settings.chat_days is not None:
        cutoff = now - timedelta(days=settings.chat_days)
        messages = _in_batches(store_id, lambda s: _prune_messages(s, cutoff))
        _record(store_id, "messages.pruned", messages, cutoff)
    return Pruned(runs, messages, turns)


def _in_batches(store_id: uuid.UUID, prune_batch) -> int:
    """Run `prune_batch` (which prunes up to `BATCH` rows in the session it's given, answering
    how many it chose and how many of the kind went) in a transaction of its own, until a batch
    comes up short. Answers how many went in all."""
    total = 0
    while True:
        with store_session(store_id) as store:
            chosen, gone, sandboxes = prune_batch(store)
            store.commit()
        total += gone
        if sandboxes:
            asyncio.run(stop_sandboxes(sandboxes))
        if chosen < BATCH:
            return total


def _record(store_id: uuid.UUID, action: str, count: int, cutoff: datetime) -> None:
    with store_session(store_id) as store:
        audit.record(
            store, audit.SYSTEM, action, details={"count": count, "cutoff": cutoff.isoformat()}
        )
        store.commit()


# --- Runs --------------------------------------------------------------------------------------


def _agent_run_in_flight(cutoff: datetime) -> ColumnElement[bool]:
    """Whether an agent run is in use, or was lately: running, held, waiting, or with a turn
    or invocation since the cutoff, or still to finish."""
    return or_(
        AgentRun.created_at >= cutoff,
        func.coalesce(AgentRun.lease_until > func.now(), false()),
        exists().where(
            Turn.agent_run_id == AgentRun.id,
            or_(
                Turn.status == AgentRunStatus.running,
                Turn.started_at >= cutoff,
                Turn.finished_at >= cutoff,
            ),
        ),
        exists().where(
            Invocation.agent_run_id == AgentRun.id,
            or_(Invocation.status.in_(LIVE), Invocation.finished_at >= cutoff),
        ),
        exists().where(SchemaChangeRequest.requested_by_run == AgentRun.id, *not_given()),
    )


def _prune_runs(store: Session, cutoff: datetime) -> tuple[int, int, list[SandboxHandle]]:
    """One batch: workflow runs first (with their agent nodes' runs), then agent runs on their
    own, up to `BATCH` in all."""
    workflow_ids = store.scalars(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.status.in_(FINISHED_WORKFLOW_RUN),
            func.coalesce(WorkflowRun.finished_at, WorkflowRun.created_at) < cutoff,
            ~exists().where(
                Invocation.workflow_run_id == WorkflowRun.id, Invocation.status.in_(LIVE)
            ),
            # None of its agent nodes waits, nor has a run still in flight.
            ~exists()
            .where(WorkflowRunStep.run_id == WorkflowRun.id)
            .where(
                or_(
                    WorkflowRunStep.waiting_on_request_id.is_not(None),
                    exists().where(
                        AgentRun.id == WorkflowRunStep.agent_run_id, _agent_run_in_flight(cutoff)
                    ),
                )
            ),
        )
        .order_by(WorkflowRun.id)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    node_runs = (
        store.scalars(
            select(WorkflowRunStep.agent_run_id).where(
                WorkflowRunStep.run_id.in_(workflow_ids),
                WorkflowRunStep.agent_run_id.is_not(None),
            )
        ).all()
        if workflow_ids
        else []
    )
    room = BATCH - len(workflow_ids)
    agent_ids = (
        store.scalars(
            select(AgentRun.id)
            .where(
                ~_agent_run_in_flight(cutoff),
                # An agent node's run goes with its workflow run.
                ~exists().where(WorkflowRunStep.agent_run_id == AgentRun.id),
            )
            .order_by(AgentRun.id)
            .limit(room)
            .with_for_update(skip_locked=True)
        ).all()
        if room
        else []
    )
    sandboxes = _delete_runs(store, [*agent_ids, *node_runs])
    if workflow_ids:
        store.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(workflow_ids)))  # and steps
    chosen = len(workflow_ids) + len(agent_ids)
    return chosen, chosen, sandboxes


def _delete_runs(store: Session, run_ids: list[int]) -> list[SandboxHandle]:
    """Delete these agent runs of the store, with their threads, turns (and their search
    entries) and sandbox sessions; answers their sandboxes, to stop once this commits."""
    if not run_ids:
        return []
    sandboxes = run_sandboxes(store, run_ids)
    checkpoints.delete_threads(store, run_ids)
    store.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
    return sandboxes


# --- Turns of conversation runs ----------------------------------------------------------------


def _run_idle() -> ColumnElement[bool]:
    """Whether an agent run is idle now: no turn running or holding it, no invocation
    unfinished, not waiting on a schema change request."""
    return ~or_(
        func.coalesce(AgentRun.lease_until > func.now(), false()),
        exists().where(Turn.agent_run_id == AgentRun.id, Turn.status == AgentRunStatus.running),
        exists().where(Invocation.agent_run_id == AgentRun.id, Invocation.status.in_(LIVE)),
        exists().where(SchemaChangeRequest.requested_by_run == AgentRun.id, *not_given()),
    )


def _turn_old(cutoff: datetime) -> ColumnElement[bool]:
    return and_(
        Turn.status != AgentRunStatus.running,
        Turn.started_at < cutoff,
        func.coalesce(Turn.finished_at < cutoff, false()),
    )


def _prune_turns(
    store: Session, cutoff: datetime, after: list[int]
) -> tuple[int, int, list[SandboxHandle]]:
    """One batch of up to `BATCH` idle conversation runs, after the run `after[0]` (which it
    moves on, so each run is visited once a prune and runs none can go from never hold up the
    rest), whose oldest turn is older than the cutoff: from each, its oldest turns that are
    (`_prune_run_turns`). Answers how many runs it chose, and how many turns went."""
    oldest = (
        select(Turn.id)
        .where(Turn.agent_run_id == AgentRun.id)
        .order_by(Turn.id)
        .limit(1)
        .scalar_subquery()
    )
    run_ids = store.scalars(
        select(AgentRun.id)
        .where(
            AgentRun.id > after[0],
            AgentRun.chat_id.is_not(None),
            _run_idle(),
            exists().where(Turn.id == oldest, _turn_old(cutoff)),
        )
        .order_by(AgentRun.id)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    if run_ids:
        after[0] = run_ids[-1]
    gone = sum(_prune_run_turns(store, run_id, cutoff) for run_id in run_ids)
    return len(run_ids), gone, []


def _prune_run_turns(store: Session, run_id: int, cutoff: datetime) -> int:
    """Prune the store's run's oldest turns older than the cutoff, the run's row locked: the
    turns (with their search entries), their messages from the run's history, and the
    summaries that covered only them. The cut is made only before a person's message (or at the
    end of the history). How many turns went."""
    rows = store.execute(
        select(Turn.id, Turn.message_end, _turn_old(cutoff).label("old"))
        .where(Turn.agent_run_id == run_id)
        .order_by(Turn.id)
    ).all()
    prefix = []
    for row in rows:
        if not row.old:
            break
        prefix.append(row)
    if len(prefix) == len(rows):
        # Every turn is old, but the run is in use lately (else it went whole): its latest stays.
        prefix.pop()
    messages, _ = checkpoints.stored_history(store, run_id)
    cut = 0
    while prefix:
        ends = [r.message_end for r in prefix if r.message_end is not None]
        cut = min(max(ends, default=0), len(messages))
        if cut == len(messages) or isinstance(messages[cut], HumanMessage):
            break
        prefix.pop()
    if not prefix:
        return 0
    turn_ids = [r.id for r in prefix]
    store.execute(delete(Turn).where(Turn.id.in_(turn_ids)))  # and their search entries
    if cut:
        checkpoints.drop_oldest(store, run_id, cut)
        store.execute(
            update(Turn)
            .where(Turn.agent_run_id == run_id, Turn.message_end.is_not(None))
            .values(message_end=func.greatest(Turn.message_end - cut, 0))
        )
        store.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(message_count=func.greatest(AgentRun.message_count - cut, 0))
        )
        # Each run message's place, in two steps so no two ever share one on the way.
        run_messages = (SearchItem.agent_run_id == run_id, SearchItem.turn_id.is_not(None))
        store.execute(delete(SearchItem).where(*run_messages, SearchItem.position < cut))
        store.execute(
            update(SearchItem).where(*run_messages).values(position=-SearchItem.position - 1)
        )
        store.execute(
            update(SearchItem).where(*run_messages).values(position=-SearchItem.position - 1 - cut)
        )
    # A summary goes once every turn it covers has.
    store.execute(
        delete(RunSummary).where(
            RunSummary.agent_run_id == run_id,
            ~exists().where(
                Turn.agent_run_id == run_id,
                or_(RunSummary.last_turn_id.is_(None), Turn.id <= RunSummary.last_turn_id),
            ),
        )
    )
    return len(turn_ids)


# --- Messages ----------------------------------------------------------------------------------


def _prune_messages(store: Session, cutoff: datetime) -> tuple[int, int, list[SandboxHandle]]:
    """One batch of messages older than the cutoff whose invocations are done with, and the
    cards and results of the invocations they started, which go with those."""
    own = aliased(Invocation)
    started = aliased(Invocation)
    message_ids = store.scalars(
        select(ChatMessage.id)
        .where(
            ChatMessage.created_at < cutoff,
            # Its own invocation (a card's or a result's) is finished.
            ~exists().where(
                own.id == ChatMessage.invocation_id,
                or_(own.status.in_(LIVE), own.finished_at >= cutoff),
            ),
            # And so is every one it started, before the cutoff.
            ~exists().where(
                started.message_id == ChatMessage.id,
                or_(started.status.in_(LIVE), started.finished_at >= cutoff),
            ),
        )
        .order_by(ChatMessage.id)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    if not message_ids:
        return 0, 0, []
    # The invocations they started go with them (a foreign key's cascade), and with those their
    # cards and results: counted here, as they go too.
    attached = select(ChatMessage.id).where(
        ChatMessage.invocation_id.in_(
            select(Invocation.id).where(Invocation.message_id.in_(message_ids))
        )
    )
    gone = store.execute(
        delete(ChatMessage)
        .where(or_(ChatMessage.id.in_(message_ids), ChatMessage.id.in_(attached)))
        .returning(ChatMessage.id)
    ).all()
    return len(message_ids), len(gone), []
