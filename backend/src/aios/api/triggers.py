"""Triggers: what starts agents and workflows without a user in the loop.

Schedules are the only kind so far. Each targets one agent (a new run of its current version,
starting with the trigger's `input` as the user's message) or one workflow (a new run of its
current version, with `input` as the run's input). `aios.scheduler` fires them.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from aios import cron
from aios.api.skills import lock_live
from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import (
    Agent,
    Trigger,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
)
from aios.workflows import fail_stopped_runs

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


class TriggerOut(BaseModel):
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


def last_outcome(trigger: Trigger) -> tuple[TriggerStatus | None, str | None]:
    """How the trigger last went: once it has started a workflow run, how that run is going."""
    run = trigger.last_workflow_run
    if run is None:
        return trigger.last_status, trigger.last_error
    return RUN_STATUS[run.status], run.error if run.status == WorkflowRunStatus.failed else None


def trigger_out(trigger: Trigger) -> TriggerOut:
    target = trigger.agent if trigger.target_kind == TriggerTargetKind.agent else trigger.workflow
    last_status, last_error = last_outcome(trigger)
    return TriggerOut(
        id=trigger.id,
        kind=trigger.kind,
        target_kind=trigger.target_kind,
        target_id=target.id,
        target_name=target.name,
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
    )


def get_owned_trigger(db: Session, user_id: int, trigger_id: int) -> Trigger:
    trigger = db.get(Trigger, trigger_id)
    if trigger is None or trigger.owner_id != user_id:
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


def apply(db: Session, user_id: int, trigger: Trigger, body: TriggerIn) -> None:
    """Set the trigger from the request, checked, and schedule its next run."""
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
    target = db.get(model, body.target_id)
    # Share-locked until commit, so a concurrent delete of the target (which deletes its
    # triggers) either waits and then sees this one, or wins and is seen here.
    if (
        target is None
        or target.owner_id != user_id
        or not lock_live(db, model, [target.id], read=True)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {body.target_kind.value}"
        )
    trigger.kind = body.kind
    trigger.target_kind = body.target_kind
    trigger.agent_id = target.id if model is Agent else None
    trigger.workflow_id = target.id if model is Workflow else None
    trigger.enabled = body.enabled
    trigger.input = body.input
    trigger.cron = expr
    trigger.timezone = tz
    trigger.next_run_at = cron.next_run(expr, tz, datetime.now(UTC)) if body.enabled else None


LOAD = (
    selectinload(Trigger.agent),
    selectinload(Trigger.workflow),
    selectinload(Trigger.last_workflow_run),
)


def fail_stopped_last_runs(db: Session, *where) -> None:
    """Fail the last workflow runs of the triggers matching `where` whose worker died, so they
    aren't shown running for ever (as `api.workflows.get_workflow_run` does); commits."""
    fail_stopped_runs(db, WorkflowRun.id.in_(select(Trigger.last_workflow_run_id).where(*where)))


def cancel_unstarted_firing(db: Session, *where) -> None:
    """Drop the firing of each trigger matching `where` if it has been claimed but hasn't
    started its run, as it's edited: that firing was for the old settings, and pausing must stop it.

    It's cleared as if it never fired (its previous outcome was already replaced when it was
    claimed), which `aios.scheduler` sees as it having fired again, and so skips. A firing that
    has started its run is left to finish and record how it went. The scheduler holds the
    trigger's row lock while it starts the run, so this waits for that and then re-checks.
    """
    db.execute(
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


def load(db: Session, trigger_id: int) -> Trigger:
    fail_stopped_last_runs(db, Trigger.id == trigger_id)
    db.expire_all()
    return db.scalars(select(Trigger).where(Trigger.id == trigger_id).options(*LOAD)).one()


def disable_triggers(db: Session, condition) -> None:
    """Turn off the triggers matching `condition`, as pausing each would: none fires again until
    it's turned back on, and a firing claimed but not started is dropped. Lock their targets
    first (see `aios.scheduler._claimed`)."""
    db.execute(
        update(Trigger)
        .where(condition)
        .values(enabled=False, next_run_at=None)
        .execution_options(synchronize_session=False)
    )
    cancel_unstarted_firing(db, condition)


def delete_triggers(db: Session, condition) -> None:
    """Delete the triggers matching `condition`, e.g. as their agent or workflow is deleted (it's
    only soft-deleted, so they wouldn't cascade)."""
    db.execute(delete(Trigger).where(condition))


@router.get("/triggers", response_model=list[TriggerOut])
def list_triggers(
    user: CurrentUser,
    db: DB,
    target_kind: TriggerTargetKind | None = None,
    target_id: DbId | None = None,
):
    """The user's triggers, the next to fire first; disabled ones last. Optionally only one
    target's: an agent's or workflow's, so both `target_kind` and `target_id` or neither."""
    if (target_kind is None) != (target_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Filter by both target_kind and target_id, or neither",
        )
    fail_stopped_last_runs(db, Trigger.owner_id == user.id)
    query = (
        select(Trigger)
        .where(Trigger.owner_id == user.id)
        .options(*LOAD)
        .order_by(Trigger.next_run_at.asc().nulls_last(), Trigger.id)
    )
    if target_kind == TriggerTargetKind.agent:
        query = query.where(Trigger.agent_id == target_id)
    elif target_kind == TriggerTargetKind.workflow:
        query = query.where(Trigger.workflow_id == target_id)
    return [trigger_out(t) for t in db.scalars(query)]


@router.post("/triggers", response_model=TriggerOut, status_code=status.HTTP_201_CREATED)
def create_trigger(body: TriggerIn, user: CurrentUser, db: DB):
    trigger = Trigger(owner_id=user.id)
    apply(db, user.id, trigger, body)
    db.add(trigger)
    db.commit()
    return trigger_out(load(db, trigger.id))


@router.post("/triggers/preview", response_model=PreviewOut)
def preview_schedule(body: PreviewIn, user: CurrentUser):
    """What a schedule means: its description and next few runs (for the form, as it's typed)."""
    expr, tz = checked_schedule(body.cron, body.timezone)
    return PreviewOut(
        description=cron.describe(expr),
        next_runs=cron.next_runs(expr, tz, datetime.now(UTC), PREVIEW_RUNS),
    )


@router.get("/triggers/timezones", response_model=list[str])
def list_timezones(user: CurrentUser):
    return sorted(cron.timezones())


@router.get("/triggers/{trigger_id}", response_model=TriggerOut)
def get_trigger(trigger_id: DbId, user: CurrentUser, db: DB):
    return trigger_out(load(db, get_owned_trigger(db, user.id, trigger_id).id))


@router.put("/triggers/{trigger_id}", response_model=TriggerOut)
def update_trigger(trigger_id: DbId, body: TriggerIn, user: CurrentUser, db: DB):
    """Replace the trigger's settings; its next run is worked out again from now. A firing that
    hasn't started its run yet is dropped (see `cancel_unstarted_firing`)."""
    trigger = get_owned_trigger(db, user.id, trigger_id)
    apply(db, user.id, trigger, body)
    cancel_unstarted_firing(db, Trigger.id == trigger.id)
    db.commit()
    return trigger_out(load(db, trigger.id))


@router.delete("/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trigger(trigger_id: DbId, user: CurrentUser, db: DB) -> None:
    db.delete(get_owned_trigger(db, user.id, trigger_id))
    db.commit()
