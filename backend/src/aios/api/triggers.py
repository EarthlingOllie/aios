"""Triggers: what starts agents and workflows without a user in the loop.

Schedules are the only kind so far. Each targets one agent (a new run of its current version,
starting with the trigger's `input` as the user's message) or one workflow (a new run of its
current version, with `input` as the run's input). `aios.scheduler` fires them.

A trigger is its organisation's, like its target, and is in its store beside it: every member
sees, changes and deletes every one, and each says which member made it and which last changed
it. Once the store has committed a change, the platform's schedule due index is written
(`aios.schedules`), which is how the scheduler finds what's due.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aios import audit, cron, schedules
from aios.api.provenance import Provenance, UserOut, built_by, people, provenance
from aios.api.skills import lock_live
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import store_id_of
from aios.datastores.models import Agent, Trigger, Workflow, WorkflowRun
from aios.db import DB, DbId
from aios.models import (
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
    WorkflowRunStatus,
)
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")

PREVIEW_RUNS = 5


class TriggerIn(BaseModel):
    kind: Literal[TriggerKind.schedule] = TriggerKind.schedule
    target_kind: TriggerTargetKind
    target_id: DbId
    enabled: bool = True
    # The message an agent run starts with, or a workflow run's input; required.
    input: str = Field("", max_length=100_000)
    cron: str = Field(max_length=255)
    timezone: str = Field("UTC", max_length=64)


class TriggerOut(Provenance):
    id: int
    kind: TriggerKind
    target_kind: TriggerTargetKind
    target_id: int
    target_name: str
    enabled: bool
    input: str
    cron: str
    # The cron expression in plain English, e.g. "At 09:00 on Monday".
    description: str
    timezone: str
    next_run_at: datetime | None
    last_fired_at: datetime | None
    # For a workflow, its last run's: a worker runs it after the schedule has fired.
    last_status: TriggerStatus | None
    last_error: str | None
    # The agent run (agents) or workflow run (workflows) it last started.
    last_run_id: int | None
    last_workflow_run_id: int | None
    created_at: datetime
    updated_at: datetime


class PreviewIn(BaseModel):
    cron: str = Field(max_length=255)
    timezone: str = Field("UTC", max_length=64)


class PreviewOut(BaseModel):
    description: str
    next_runs: list[datetime]


RUN_STATUS = {
    WorkflowRunStatus.queued: TriggerStatus.running,
    WorkflowRunStatus.running: TriggerStatus.running,
    # Still going: paused for a schema change decision.
    WorkflowRunStatus.waiting: TriggerStatus.running,
    WorkflowRunStatus.succeeded: TriggerStatus.succeeded,
    WorkflowRunStatus.failed: TriggerStatus.failed,
}


# A workflow run's status and error, by its id in the organisation's store.
LastRuns = dict[int, tuple[WorkflowRunStatus, str | None]]


def last_workflow_runs(store: Session, triggers: Sequence[Trigger]) -> LastRuns:
    """How the workflow runs these triggers last started are going, from their store. Those
    whose worker died are failed first, so they aren't shown running for ever (as
    `api.workflows.get_workflow_run` does)."""
    ids = [t.last_workflow_run_id for t in triggers if t.last_workflow_run_id is not None]
    if not ids:
        return {}
    rows = store.execute(
        select(WorkflowRun.id, WorkflowRun.status, WorkflowRun.error).where(WorkflowRun.id.in_(ids))
    ).all()
    return {run_id: (status, error) for run_id, status, error in rows}


def last_outcome(trigger: Trigger, runs: LastRuns) -> tuple[TriggerStatus | None, str | None]:
    """How the trigger last went: once it has started a workflow run, how that run is going
    (from `runs`, `last_workflow_runs`)."""
    run = runs.get(trigger.last_workflow_run_id)
    if run is None:
        return trigger.last_status, trigger.last_error
    status, error = run
    return RUN_STATUS[status], error if status == WorkflowRunStatus.failed else None


def target_id(trigger: Trigger) -> int:
    """The id of the trigger's agent or workflow, in its organisation's store."""
    return (
        trigger.agent_id if trigger.target_kind == TriggerTargetKind.agent else trigger.workflow_id
    )


def target_names(
    store: Session, triggers: Sequence[Trigger]
) -> dict[tuple[TriggerTargetKind, int], str]:
    """The names of these triggers' targets, from their store, by (kind, id)."""
    found: dict[tuple[TriggerTargetKind, int], str] = {}
    for kind, model in ((TriggerTargetKind.agent, Agent), (TriggerTargetKind.workflow, Workflow)):
        ids = {target_id(t) for t in triggers if t.target_kind == kind}
        if ids:
            for i, name in store.execute(select(model.id, model.name).where(model.id.in_(ids))):
                found[kind, i] = name
    return found


def trigger_out(
    db: Session,
    store: Session,
    organisation_id: int,
    trigger: Trigger,
    known: dict[int, UserOut] | None = None,
    names: dict[tuple[TriggerTargetKind, int], str] | None = None,
    runs: LastRuns | None = None,
) -> TriggerOut:
    """The organisation's trigger (in its store, `store`), with who made and last changed it
    from `known` (`provenance.people`), its target's name from `names` (`target_names`) and its
    last workflow run from `runs` (`last_workflow_runs`), else read now."""
    if known is None:
        known = people(db, organisation_id, [trigger.created_by, trigger.updated_by])
    if names is None:
        names = target_names(store, [trigger])
    if runs is None:
        runs = last_workflow_runs(store, [trigger])
    last_status, last_error = last_outcome(trigger, runs)
    return TriggerOut(
        id=trigger.id,
        kind=trigger.kind,
        target_kind=trigger.target_kind,
        target_id=target_id(trigger),
        target_name=names.get((trigger.target_kind, target_id(trigger)), ""),
        enabled=trigger.enabled,
        input=trigger.input,
        cron=trigger.cron,
        description=cron.describe(trigger.cron),
        timezone=trigger.timezone,
        next_run_at=trigger.next_run_at,
        last_fired_at=trigger.last_fired_at,
        last_status=last_status,
        last_error=last_error,
        last_run_id=trigger.last_run_id,
        last_workflow_run_id=trigger.last_workflow_run_id,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
        **provenance(db, trigger, known),
    )


def get_org_trigger(store: Session, trigger_id: int, *, lock: bool = False) -> Trigger:
    """The trigger, from the organisation's store; a 404 otherwise. With `lock`, locked until
    the store's transaction ends, for a change to it: the scheduler holds the lock while it
    claims or starts a firing of it (`aios.scheduler`)."""
    query = select(Trigger).where(Trigger.id == trigger_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    trigger = store.scalars(query).one_or_none()
    if trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return trigger


def checked_schedule(expr: str, tz: str) -> tuple[str, str]:
    """The expression (normalised) and timezone; a 422 if either is invalid."""
    expr = cron.normalize(expr)
    try:
        cron.next_run(expr, tz, datetime.now(UTC))
    except cron.CronError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return expr, tz


def apply(store: Session, user_id: int, trigger: Trigger, body: TriggerIn) -> None:
    """Set the trigger from the request (made by `user_id`), checked, and schedule its next run.
    Its target must be one of the organisation's, in its store, `store`, whose share lock on the
    target is held until the caller ends `store`'s transaction."""
    expr, tz = checked_schedule(body.cron, body.timezone)
    if not body.input.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A schedule for an agent needs a message to start its run with."
            if body.target_kind == TriggerTargetKind.agent
            # As `POST /api/workflows/{id}/runs` requires.
            else "A schedule for a workflow needs an input to start its run with.",
        )
    model = Agent if body.target_kind == TriggerTargetKind.agent else Workflow
    target = store.get(model, body.target_id)
    if target is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {body.target_kind.value}"
        )
    # An archived target starts nothing, so its schedules can be changed only while paused.
    # Share-locked until the store's transaction ends, with the trigger's, so a concurrent
    # archive of the target (which pauses its triggers) either waits and then pauses this one,
    # or wins and is seen here.
    if not lock_live(store, model, [target.id], read=True) and body.enabled:
        store.refresh(target)
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"The {body.target_kind.value} {target.name} is archived."
        )
    trigger.kind = body.kind
    trigger.target_kind = body.target_kind
    trigger.agent_id = target.id if model is Agent else None
    trigger.workflow_id = target.id if model is Workflow else None
    trigger.enabled = body.enabled
    trigger.input = body.input
    trigger.cron = expr
    trigger.timezone = tz
    trigger.updated_by = user_id
    trigger.next_run_at = cron.next_run(expr, tz, datetime.now(UTC)) if body.enabled else None


def cancel_unstarted_firing(store: Session, *where) -> None:
    """Drop the firing of each trigger matching `where` if it has been claimed but hasn't
    started its run, as it's edited: that firing was for the old settings, and pausing must stop it.

    It's cleared as if it never fired (its previous outcome was already replaced when it was
    claimed), which `aios.scheduler` sees as it having fired again, and so skips. A firing that
    has started its run is left to finish and record how it went. The scheduler holds the
    trigger's row lock while it starts the run, so this waits for that and then re-checks.
    """
    store.execute(
        update(Trigger)
        .where(
            *where,
            Trigger.last_status == TriggerStatus.running,
            Trigger.last_run_id.is_(None),
            Trigger.last_workflow_run_id.is_(None),
        )
        .values(last_fired_at=None, last_status=None, last_error=None)
        .execution_options(synchronize_session=False)
    )


def load(store: Session, trigger_id: int) -> Trigger:
    store.expire_all()
    return store.scalars(select(Trigger).where(Trigger.id == trigger_id)).one()


def disable_triggers(store: Session, condition, user_id: int) -> list[int]:
    """Turn off the store's triggers matching `condition`, as pausing each would (and as the
    member `user_id` changing them): none fires again until it's turned back on, and a firing
    claimed but not started is dropped. Lock their targets first (see
    `aios.scheduler._claimed`). Returns their ids, to take out of the due index once the store
    has committed (`unindex`)."""
    ids = list(
        store.scalars(
            update(Trigger)
            .where(condition)
            .values(enabled=False, next_run_at=None, updated_by=user_id)
            .returning(Trigger.id)
            .execution_options(synchronize_session=False)
        )
    )
    cancel_unstarted_firing(store, condition)
    return ids


def reindex(store: Session, triggers: Sequence[Trigger]) -> None:
    """Write these triggers' rows of the due index as the store has just committed them."""
    schedules.index(store_id_of(store), [(t.id, schedules.due_at(t)) for t in triggers])


def _described(trigger: Trigger) -> dict:
    """What an audit entry says of a schedule: what it starts, when, and whether it's on."""
    return {
        "target_kind": trigger.target_kind.value,
        "target_id": trigger.agent_id or trigger.workflow_id,
        "cron": trigger.cron,
        "timezone": trigger.timezone,
        "enabled": trigger.enabled,
    }


def unindex(store: Session, trigger_ids: Sequence[int]) -> None:
    """Take these triggers (disabled or deleted, committed in the store) out of the due
    index."""
    schedules.index(store_id_of(store), [(i, None) for i in trigger_ids])


@router.get("/triggers", response_model=list[TriggerOut], dependencies=[allow(Action.read)])
def list_triggers(
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
    target_kind: TriggerTargetKind | None = None,
    target_id: DbId | None = None,
):
    """The organisation's triggers, the next to fire first; disabled ones last. Optionally only one
    target's: an agent's or workflow's, so both `target_kind` and `target_id` or neither."""
    if (target_kind is None) != (target_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Filter by both target_kind and target_id, or neither",
        )
    query = select(Trigger).order_by(Trigger.next_run_at.asc().nulls_last(), Trigger.id)
    if target_kind == TriggerTargetKind.agent:
        query = query.where(Trigger.agent_id == target_id)
    elif target_kind == TriggerTargetKind.workflow:
        query = query.where(Trigger.workflow_id == target_id)
    triggers = store.scalars(query).all()
    known = people(db, org.id, [i for t in triggers for i in (t.created_by, t.updated_by)])
    names = target_names(store, triggers)
    runs = last_workflow_runs(store, triggers)
    return [trigger_out(db, store, org.id, t, known, names, runs) for t in triggers]


@router.post(
    "/triggers",
    response_model=TriggerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.build)],
)
def create_trigger(
    body: TriggerIn, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    trigger = Trigger(**built_by(user.id))
    apply(store, user.id, trigger, body)
    store.add(trigger)
    audit.configured(store, user.id, "schedule", trigger, "created", **_described(trigger))
    store.commit()
    reindex(store, [trigger])
    return trigger_out(db, store, org.id, load(store, trigger.id))


@router.post("/triggers/preview", response_model=PreviewOut, dependencies=[allow(Action.read)])
def preview_schedule(body: PreviewIn, org: CurrentOrganisation):
    """What a schedule means: its description and next few runs (for the form, as it's typed)."""
    expr, tz = checked_schedule(body.cron, body.timezone)
    return PreviewOut(
        description=cron.describe(expr),
        next_runs=cron.next_runs(expr, tz, datetime.now(UTC), PREVIEW_RUNS),
    )


@router.get("/triggers/timezones", response_model=list[str])
def list_timezones(user: CurrentUser):
    return sorted(cron.timezones())


@router.get("/triggers/{trigger_id}", response_model=TriggerOut, dependencies=[allow(Action.read)])
def get_trigger(trigger_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore):
    return trigger_out(db, store, org.id, get_org_trigger(store, trigger_id))


@router.put("/triggers/{trigger_id}", response_model=TriggerOut, dependencies=[allow(Action.build)])
def update_trigger(
    trigger_id: DbId,
    body: TriggerIn,
    user: CurrentUser,
    org: CurrentOrganisation,
    db: DB,
    store: CurrentStore,
):
    """Replace the trigger's settings; its next run is worked out again from now. A firing that
    hasn't started its run yet is dropped (see `cancel_unstarted_firing`)."""
    # Its target's lock (`apply`), then its own (writing it), as the scheduler takes them
    # (`aios.scheduler._claimed`).
    trigger = get_org_trigger(store, trigger_id)
    apply(store, user.id, trigger, body)
    store.flush()
    cancel_unstarted_firing(store, Trigger.id == trigger.id)
    audit.configured(store, user.id, "schedule", trigger, "updated", **_described(trigger))
    store.commit()
    reindex(store, [trigger])
    return trigger_out(db, store, org.id, load(store, trigger.id))


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_trigger(
    trigger_id: DbId, user: CurrentUser, org: CurrentOrganisation, store: CurrentStore
) -> None:
    """Delete the trigger. The runs it started stay, no longer naming it (their foreign key
    clears it, leaving their `updated_at`, which listings sort by)."""
    trigger = get_org_trigger(store, trigger_id, lock=True)
    audit.configured(store, user.id, "schedule", trigger, "deleted", **_described(trigger))
    store.delete(trigger)
    store.commit()
    unindex(store, [trigger_id])
