"""A workflow run's ending (finished, failed or cancelled), what it waits on, and the helpers
its advances share (`aios.workflows.advance`)."""

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, false, func, or_, select, update
from sqlalchemy.orm import Session

from aios import invocations
from aios.config import get_settings
from aios.datastores import schema_changes, store_id_of, store_session
from aios.datastores.models import (
    SchemaChangeRequest,
    WorkflowRun,
    WorkflowRunStep,
)
from aios.db import SessionLocal
from aios.models import (
    DataStore,
    SchemaChangeStatus,
    WorkflowRunStatus,
    WorkflowStepStatus,
)
from aios.workflows.validation import Graph, load_graph

log = logging.getLogger(__package__)


def alive() -> ColumnElement[bool]:
    """Whether a run is still going: running. (One whose run service items stopped and kept
    stopping is failed when their attempts run out, `aios.run_service.work`.) Never null, so it
    can be negated."""
    return func.coalesce(WorkflowRun.status == WorkflowRunStatus.running, false())


def _organisation(data_store_id: uuid.UUID) -> int | None:
    with SessionLocal() as db:
        return db.scalar(
            select(DataStore.owner_organisation_id).where(DataStore.id == data_store_id)
        )


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


def decided_wait(store: Session, step: WorkflowRunStep) -> bool:
    """Whether the waiting agent step waits on a request (of the step's store, `store`) that's
    been decided but not yet given to it: it's resumed then."""
    if step.agent_run_id is None and step.waiting_on_request_id is None:
        return False
    agent_run_ids = [step.agent_run_id] if step.agent_run_id is not None else []
    request_ids = [step.waiting_on_request_id] if step.waiting_on_request_id is not None else []
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


def _load(
    data_store_id: uuid.UUID, organisation_id: int, version_id: int, strict: bool = True
) -> Graph:
    with store_session(data_store_id, "ro") as store:
        return load_graph(store, version_id, organisation_id, strict=strict)


# A run that hasn't ended: queued, running, or waiting for a decision.
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
    *,
    output_json: Any = None,
) -> Ended:
    """End the store's runs matching `where` that haven't ended, and fail their steps still
    running, close out the requests their agent nodes made (`close_requests`), and finish the
    invocations that started them, posting their results back to their chats; in `store`'s
    transaction. A waiting run is left be: its advance makes it running before ending it. Once
    the caller has committed, `_record_ended` records the rest, on the platform."""
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
                output_json=output_json,
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
    # Steps whose nodes were stopped with the run, e.g. when it took too long or its service
    # stopped:
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
    the notifications of the requests cancelled with them go."""
    if not ended.runs:
        return
    schema_changes.clear_notifications_of(ended.cancelled)


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


def finish_run(
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


def fail_run(data_store_id: uuid.UUID, run_id: int, error: str) -> None:
    """Fail the store's run that couldn't finish, e.g. one stopped for taking too long."""
    finish_run(data_store_id, run_id, WorkflowRunStatus.failed, None, error)
