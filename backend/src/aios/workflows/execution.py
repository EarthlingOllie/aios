"""Executing a workflow run in the worker: claiming it, its heartbeat, pausing it while it waits,
and ending it (finished, failed or cancelled)."""

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import ColumnElement, and_, false, func, or_, select, update
from sqlalchemy.orm import Session

from aios import invocations

# Read on the package at call time, where tests replace them.
from aios import workflows as package
from aios.config import get_settings
from aios.datastores import history, schema_changes, store_id_of, store_session
from aios.datastores.models import (
    SchemaChangeRequest,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)
from aios.db import SessionLocal
from aios.models import (
    DataStore,
    SchemaChangeStatus,
    WorkflowRunStatus,
    WorkflowStepStatus,
)
from aios.ui_stream import error_text
from aios.workflows.steps import NodeWaiting, RecordedStep, RunState, StepFailed, _run_graph
from aios.workflows.validation import Graph, RunError, load_graph

log = logging.getLogger(__package__)


# How often a worker refreshes the heartbeat of the run it's running, and how long without one
# until the run counts as stopped: a few missed beats, so a slow write doesn't fail a live run.
# (The beats are off the event loop, so a busy one doesn't delay them. One stuck for good is
# caught by the heartbeat too: see `_Heartbeat` and `Settings.workflow_run_no_progress_limit`.)
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

# Why a run is failed once it's found running without a live worker.
WORKER_STOPPED = "The worker stopped while running this, so it couldn't finish."


def alive() -> ColumnElement[bool]:
    """Whether a run is still going: running, with a worker that has beaten lately. Never null,
    so it can be negated: a running run that isn't alive has lost its worker."""
    return and_(
        WorkflowRun.status == WorkflowRunStatus.running,
        func.coalesce(WorkflowRun.heartbeat_at > func.now() - package.STALE_AFTER, false()),
    )


def fail_stopped_runs(store: Session, *where: ColumnElement[bool]) -> None:
    """Fail the runs matching `where` (in the store `store` is a session on) that are running
    but not alive (see `alive`), and their steps that were cut off; commits."""
    ended = _end_runs(
        store,
        [*where, WorkflowRun.status == WorkflowRunStatus.running, ~alive()],
        WorkflowRunStatus.failed,
        None,
        WORKER_STOPPED,
    )
    store.commit()
    _record_ended(ended)


@dataclass(frozen=True)
class Claimed:
    """A run a worker has claimed to run, from queued or waiting."""

    state: RunState
    version_id: int
    input: str
    # What's left of its time limit: waiting doesn't count (seconds).
    time_left: float


def _organisation(data_store_id: uuid.UUID) -> int | None:
    with SessionLocal() as db:
        return db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )


def _claim(data_store_id: uuid.UUID, run_id: int) -> Claimed | None:
    """Mark the store's queued or waiting run running; return it. None if it's neither
    (deleted, ended, or already running), or if it's failed because it couldn't start.

    A waiting run is queued again once a request it waits on is decided (`enqueue_resume`);
    claimed, it adds the time it waited to `waited_seconds` and carries on from the steps it
    recorded (`RunState.recorded`).

    A run is queued once, and its task acked only when it ends, so a run found running was
    started by an earlier delivery. If that's still going (its heartbeat is fresh), this one
    leaves it be: the broker redelivers a message it's held longer than its visibility timeout
    (see `aios.worker`), which a run can outlast. If not, its worker died without finishing it
    (a hard time limit, OOM kill, restart); it can't go on from where it stopped, so it fails.
    """
    waited = func.extract("epoch", func.now() - WorkflowRun.waiting_since)
    ran = func.extract("epoch", func.now() - WorkflowRun.started_at) - WorkflowRun.waited_seconds
    with store_session(data_store_id) as store:
        row = store.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_([WorkflowRunStatus.queued, WorkflowRunStatus.waiting]),
            )
            .values(
                status=WorkflowRunStatus.running,
                started_at=func.coalesce(WorkflowRun.started_at, func.now()),
                heartbeat_at=func.now(),
                waited_seconds=WorkflowRun.waited_seconds + func.coalesce(waited, 0),
                waiting_since=None,
            )
            # After the update: `ran` is its running time so far.
            .returning(
                WorkflowRun.workflow_version_id,
                WorkflowRun.input,
                WorkflowRun.created_by,
                ran,
            )
        ).one_or_none()
        if row is None:
            fail_stopped_runs(store, WorkflowRun.id == run_id)
            return None
        version_id, text, created_by, ran_seconds = row
        recorded = {
            tuple(step.path): RecordedStep(
                id=step.id,
                status=step.status,
                input=step.input,
                output=step.output,
                agent_run_id=step.agent_run_id,
            )
            for step in store.scalars(
                select(WorkflowRunStep).where(WorkflowRunStep.run_id == run_id)
            )
        }
        # In the claim's transaction: if its workflow can't be read, or its organisation has
        # gone, the run isn't claimed...
        try:
            organisation_id = package._organisation(data_store_id)
            if organisation_id is None:
                raise RunError("The run's organisation has been deleted.")
            workflow_id, name, version = store.execute(
                select(Workflow.id, Workflow.name, WorkflowVersion.version)
                .join(WorkflowVersion, WorkflowVersion.workflow_id == Workflow.id)
                .where(WorkflowVersion.id == version_id)
            ).one()
        except Exception as exc:
            store.rollback()
            error = exc
        else:
            error = None
            store.commit()
    if error is not None:
        # ...and it's failed now, with the real error: nothing else would ever run or end it. A
        # waiting run (being resumed) is failed too, rather than left waiting for good.
        log.exception("workflow run %s: couldn't be claimed", run_id, exc_info=error)
        with store_session(data_store_id) as store:
            store.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.waiting)
                .values(status=WorkflowRunStatus.running, waiting_since=None)
            )
            store.commit()
        fail_run(data_store_id, run_id, error_text(error))
        return None
    state = RunState(
        id=run_id,
        organisation_id=organisation_id,
        created_by=created_by,
        workflow_id=workflow_id,
        workflow_name=name,
        workflow_version=version,
        data_store_id=data_store_id,
        recorded=recorded,
    )
    time_left = get_settings().workflow_run_time_limit - float(ran_seconds)
    return Claimed(state, version_id, text, time_left)


def _not_given(
    agent_run_ids: Iterable[int], request_ids: Iterable[uuid.UUID]
) -> list[ColumnElement[bool]]:
    """The store's requests made by these agent runs (workflow nodes' conversations), or with
    these ids, not yet given to the runs that made them."""
    agent_run_ids, request_ids = list(agent_run_ids), list(request_ids)
    return [
        or_(
            SchemaChangeRequest.requested_by_run.in_(agent_run_ids) if agent_run_ids else false(),
            SchemaChangeRequest.id.in_(request_ids) if request_ids else false(),
        ),
        SchemaChangeRequest.resumed_at.is_(None),
    ]


def _waiting_steps(store: Session, run_id: int) -> tuple[list[int], list[uuid.UUID]]:
    """The agent runs, and the requests, the run's waiting steps wait on."""
    rows = store.execute(
        select(WorkflowRunStep.agent_run_id, WorkflowRunStep.waiting_on_request_id).where(
            WorkflowRunStep.run_id == run_id,
            WorkflowRunStep.status == WorkflowStepStatus.waiting,
        )
    ).all()
    return (
        [r.agent_run_id for r in rows if r.agent_run_id is not None],
        [r.waiting_on_request_id for r in rows if r.waiting_on_request_id is not None],
    )


def _decided_waits(data_store_id: uuid.UUID, run_id: int) -> bool:
    """Whether a waiting step of the store's run waits on a request that's been decided but
    not yet given to it."""
    with store_session(data_store_id, "ro") as store:
        agent_run_ids, request_ids = _waiting_steps(store, run_id)
        if not agent_run_ids and not request_ids:
            return False
        return (
            store.scalar(
                select(SchemaChangeRequest.id)
                .where(
                    *_not_given(agent_run_ids, request_ids),
                    SchemaChangeRequest.status.not_in(
                        [SchemaChangeStatus.pending, SchemaChangeStatus.approved]
                    ),
                )
                .limit(1)
            )
            is not None
        )


def workflow_run_waiting_on(store: Session, request: SchemaChangeRequest) -> int | None:
    """The workflow run (in the request's store, `store`) whose agent node made the request, if
    one did: the run to resume once it's decided. Found by the step's agent run, or, if the step
    has lost it, by the request the step waits on."""
    by_run = (
        WorkflowRunStep.agent_run_id == request.requested_by_run
        if request.requested_by_run is not None
        else false()
    )
    return store.scalar(
        select(WorkflowRunStep.run_id)
        .where(or_(by_run, WorkflowRunStep.waiting_on_request_id == request.id))
        .limit(1)
    )


def _pause_run(data_store_id: uuid.UUID, run_id: int) -> None:
    """A run whose nodes have all ended or wait: it waits, holding no worker.

    Queued again at once if a request it waits on was decided while it ran: that decision's
    own queuing found it running, and left it. (One decided once it's waiting finds it waiting,
    and its queuing claims it.)
    """
    with store_session(data_store_id) as store:
        paused = store.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.running)
            .values(status=WorkflowRunStatus.waiting, waiting_since=func.now())
        )
        store.commit()
    if paused.rowcount > 0 and _decided_waits(data_store_id, run_id):
        from aios.worker import run_workflow_task

        run_workflow_task.delay(str(data_store_id), run_id)


def _beat(data_store_id: uuid.UUID, run_id: int) -> bool:
    """Refresh the run's heartbeat; False if it isn't running any more (ended or deleted)."""
    with store_session(data_store_id) as store:
        beat = store.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowRunStatus.running)
            .values(heartbeat_at=func.now())
        )
        store.commit()
        return beat.rowcount > 0


class _Heartbeat:
    """Refreshes a run's heartbeat on a thread of its own until stopped, when the run ends.

    Not on the event loop, so a node that holds the loop (a long blocking call, CPU-bound work)
    doesn't let a live run go stale. Cancels `run`, on its loop, if the run has ended without it:
    failed as stopped because a beat was late (`fail_stopped_runs`), or deleted. Nobody will see
    the rest of its turns.

    It also watches the loop make progress: each beat hands the loop a callback that notes when
    it ran (`_progressed`), which it only does once the loop is free. A node awaiting a slow model
    or tool call leaves the loop free, so it keeps progressing however long it waits. If none has
    run for `workflow_run_no_progress_limit`, the loop is stuck (a blocking call that doesn't
    return, a deadlock) and so is the run: it fails the run with `no_progress_error`, stops
    beating, and cancels the run once the loop is free again. (A loop that never is keeps its
    worker until Celery's hard time limit, see `aios.worker`, but the run has ended.)
    """

    def __init__(self, data_store_id: uuid.UUID, run_id: int, run: asyncio.Task) -> None:
        self.data_store_id = data_store_id
        self.run_id = run_id
        self.run = run
        self.loop = asyncio.get_running_loop()
        # When the loop last ran a callback this handed it (`time.monotonic`).
        self.progressed_at = time.monotonic()
        self.stopping = threading.Event()
        # Whether it stopped the run: set on the loop, so only once the run has been cancelled.
        self.stopped_run = False
        self.thread = threading.Thread(
            target=self._beat_until_stopped, name=f"workflow-run-{run_id}-heartbeat", daemon=True
        )
        self.thread.start()

    def _beat_until_stopped(self) -> None:
        limit = get_settings().workflow_run_no_progress_limit
        while not self.stopping.wait(package.HEARTBEAT_SECONDS):
            if time.monotonic() - self.progressed_at > limit:
                self._fail_stuck_run()
                return
            # If the loop has closed, the run is over (and this is being stopped).
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._progressed)
            try:
                alive = package._beat(self.data_store_id, self.run_id)
            except Exception:
                # The run goes on; it only counts as stopped after several missed beats.
                log.exception("workflow run %s: couldn't record its heartbeat", self.run_id)
                continue
            if not alive:
                # Also once the run has just finished itself; then by the time this is called on
                # the loop it's done or being stopped, or it's cancelled after its result was
                # written, which changes nothing. If the loop has closed, the run is over.
                with contextlib.suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self._stop_run)
                return

    def _progressed(self) -> None:
        self.progressed_at = time.monotonic()

    def _fail_stuck_run(self) -> None:
        log.error(
            "workflow run %s: its event loop has made no progress for %ss; failing it",
            self.run_id,
            get_settings().workflow_run_no_progress_limit,
        )
        try:
            fail_run(self.data_store_id, self.run_id, no_progress_error())
        except Exception:
            # Without beats, it's failed as stopped once it's read (`fail_stopped_runs`).
            log.exception("workflow run %s: couldn't record that it's stuck", self.run_id)
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._stop_run)

    def _stop_run(self) -> None:
        if self.stopping.is_set() or self.run.done():
            return
        log.warning("workflow run %s ended elsewhere; stopping it", self.run_id)
        self.stopped_run = True
        self.run.cancel()

    async def stop(self) -> None:
        """Stop beating, and wait for a beat underway: none comes once the run has ended."""
        self.stopping.set()
        await asyncio.to_thread(self.thread.join)


def _load(data_store_id, organisation_id: int, version_id: int, strict: bool = True) -> Graph:
    with store_session(data_store_id, "ro") as store:
        return load_graph(store, version_id, organisation_id, strict=strict)


# A run that hasn't ended: what a worker ends itself, and a run waiting for a decision.
NOT_ENDED = (WorkflowRunStatus.queued, WorkflowRunStatus.running, WorkflowRunStatus.waiting)


@dataclass
class Ended:
    """Runs of a store that `_end_runs` has just ended there: what the platform records of
    them once that's committed (`_record_ended`)."""

    data_store_id: uuid.UUID
    runs: list[WorkflowRun] = field(default_factory=list)
    # The requests their agent nodes made that were cancelled with them, whose notifications go.
    cancelled: list[uuid.UUID] = field(default_factory=list)


def _end_runs(
    store: Session,
    where: Sequence[ColumnElement[bool]],
    status: WorkflowRunStatus,
    output: str | None,
    error: str | None,
) -> Ended:
    """End the store's runs matching `where` that haven't ended, and fail their steps still
    running, close out the requests their agent nodes made (`close_requests`), and finish the
    invocations that started them, posting their results back to their chats; in `store`'s
    transaction. A waiting run is left be (it isn't the worker's to end). Once the caller has
    committed, `_record_ended` records the rest, on the platform."""
    ended = Ended(store_id_of(store))
    going = [s for s in NOT_ENDED if s != WorkflowRunStatus.waiting]
    waited = func.coalesce(func.extract("epoch", func.now() - WorkflowRun.waiting_since), 0)
    ended.runs = list(
        store.scalars(
            update(WorkflowRun)
            .where(*where, WorkflowRun.status.in_(going))
            .values(
                status=status,
                output=output,
                error=error,
                finished_at=func.now(),
                waited_seconds=WorkflowRun.waited_seconds + waited,
                waiting_since=None,
            )
            .returning(WorkflowRun)
        )
    )
    if not ended.runs:
        return ended
    ids = [run.id for run in ended.runs]
    # Before the steps forget the requests they wait on.
    agent_run_ids: list[int] = []
    request_ids: list[uuid.UUID] = []
    for agent_run_id, request_id in store.execute(
        select(WorkflowRunStep.agent_run_id, WorkflowRunStep.waiting_on_request_id).where(
            WorkflowRunStep.run_id.in_(ids)
        )
    ):
        if agent_run_id is not None:
            agent_run_ids.append(agent_run_id)
        if request_id is not None:
            request_ids.append(request_id)
    ended.cancelled = close_requests(store, agent_run_ids, request_ids)
    # Steps whose nodes were stopped with the run, e.g. when it took too long or its worker died:
    # they never recorded their own end. And those waiting, when another node failed meanwhile.
    store.execute(
        update(WorkflowRunStep)
        .where(
            WorkflowRunStep.run_id.in_(ids),
            WorkflowRunStep.status.in_([WorkflowStepStatus.running, WorkflowStepStatus.waiting]),
        )
        .values(
            status=WorkflowStepStatus.failed,
            error="The run stopped before this step finished.",
            finished_at=func.now(),
            waiting_on_request_id=None,
        )
    )
    # Those a chat started post their results back to it.
    invocations.finish_workflow_runs(store, ended.runs)
    return ended


def _record_ended(ended: Ended) -> None:
    """What the platform records of runs that have ended in their store, once that's committed:
    the notifications of the requests cancelled with them go, and they go in the run history
    export's outbox. Commits."""
    if not ended.runs:
        return
    schema_changes.clear_notifications_of(ended.cancelled)
    with SessionLocal() as db:
        history.enqueue_workflow_runs(db, ended.data_store_id, [r.id for r in ended.runs])
        db.commit()


def close_requests(
    store: Session, agent_run_ids: Iterable[int], request_ids: Iterable[uuid.UUID]
) -> list[uuid.UUID]:
    """Close out the store's schema change requests that workflow runs' agent nodes made (by
    their steps' agent runs, `agent_run_ids`, or the requests their steps waited on,
    `request_ids`) and haven't been given: the runs have ended or are being deleted, so nothing
    will give them their outcomes. Pending ones are cancelled, so they can't be approved
    (`schema_changes.cancel`: returns their ids, whose notifications to clear once this
    commits); the others count as given, so the conversations don't wait on them (one being
    applied still is). In the caller's transaction."""
    not_given = _not_given(agent_run_ids, request_ids)
    cancelled = schema_changes.cancel(store, *not_given)
    store.execute(update(SchemaChangeRequest).where(*not_given).values(resumed_at=func.now()))
    return cancelled


def _finish_run(
    data_store_id: uuid.UUID,
    run_id: int,
    status: WorkflowRunStatus,
    output: str | None,
    error: str | None,
) -> None:
    with store_session(data_store_id) as store:
        # A no-op if the run was deleted meanwhile, or has already finished.
        ended = _end_runs(store, [WorkflowRun.id == run_id], status, output, error)
        store.commit()
    _record_ended(ended)


def time_limit_error() -> str:
    minutes = get_settings().workflow_run_time_limit // 60
    return f"The run took longer than {minutes} minutes, so it was stopped."


def no_progress_error() -> str:
    limit = get_settings().workflow_run_no_progress_limit
    took = f"{limit // 60:g} minutes" if limit >= 120 else f"{limit:g} seconds"
    return f"The run made no progress for {took}: something held up its worker, so it was stopped."


def fail_run(data_store_id: uuid.UUID, run_id: int, error: str) -> None:
    """Fail the store's run that couldn't finish, e.g. one stopped for taking too long."""
    _finish_run(data_store_id, run_id, WorkflowRunStatus.failed, None, error)


async def execute_run(data_store_id: uuid.UUID, run_id: int) -> None:
    """Run the store's queued workflow run, or carry on a waiting one, to the end or until it
    waits, recording its steps and then its result.

    Called by the worker (`aios.worker.run_workflow_task`). Does nothing unless the run is
    queued or waiting, so a task delivered twice runs it once; a run left running by a worker
    that died is failed instead (see `_claim`). Its heartbeat is refreshed until it ends or
    waits, and stops it if it has ended elsewhere meanwhile.
    """
    claimed = await asyncio.to_thread(_claim, data_store_id, run_id)
    if claimed is None:
        return
    run = asyncio.create_task(_execute(claimed))
    heartbeat = _Heartbeat(data_store_id, run_id, run)
    try:
        await run
    except asyncio.CancelledError:
        # Stopped by its heartbeat; propagate any other cancellation (e.g. of this coroutine).
        if not heartbeat.stopped_run:
            raise
    finally:
        run.cancel()
        await heartbeat.stop()


def _failure(run: RunState, error: str) -> str:
    """Why the run failed: its first node's failure, and where."""
    error = run.error or error
    where = "/".join(run.failed_path)
    return f"{where}: {error}" if where else error


async def _execute(claimed: Claimed) -> None:
    run = claimed.state
    store_id = run.data_store_id
    try:
        # Timed here rather than by Celery's soft limit, whose exception can land inside a node
        # and fail it like any other error. Only running counts: a run resuming has what's left.
        async with asyncio.timeout(claimed.time_left):
            # Loaded again rather than trusted from when the run was started: e.g. a tool may
            # have left the library since. A run resuming fails only at a node that runs again.
            graph = await asyncio.to_thread(
                _load,
                run.data_store_id,
                run.organisation_id,
                claimed.version_id,
                not run.recorded,
            )
            output = await _run_graph(run, graph, (), claimed.input)
    except TimeoutError:
        await asyncio.to_thread(fail_run, store_id, run.id, time_limit_error())
    except NodeWaiting:
        if run.error is None:
            await asyncio.to_thread(_pause_run, store_id, run.id)
        else:
            # A node failed while another waits: the run fails, as it would have had the other
            # finished.
            await asyncio.to_thread(fail_run, store_id, run.id, _failure(run, run.error))
    except StepFailed as exc:
        await asyncio.to_thread(fail_run, store_id, run.id, _failure(run, exc.error))
    except RunError as exc:
        await asyncio.to_thread(fail_run, store_id, run.id, str(exc))
    except Exception as exc:
        log.exception("workflow run %s failed", run.id)
        await asyncio.to_thread(fail_run, store_id, run.id, error_text(exc))
    else:
        await asyncio.to_thread(
            _finish_run, store_id, run.id, WorkflowRunStatus.succeeded, output, None
        )
