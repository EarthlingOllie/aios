"""Triggers: what starts agents and workflows without a user in the loop.

Schedules are the only kind so far. Each targets one agent (a new run of its current version,
starting with the trigger's `input` as the user's message) or one workflow. `aios.scheduler`
fires them.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from aios import cron
from aios.api.skills import lock_live
from aios.auth import CurrentUser
from aios.db import DB, DbId
from aios.models import Agent, Trigger, TriggerKind, TriggerStatus, TriggerTargetKind, Workflow

router = APIRouter(prefix="/api")

PREVIEW_RUNS = 5


class TriggerIn(BaseModel):
    kind: Literal[TriggerKind.schedule] = TriggerKind.schedule
    target_kind: TriggerTargetKind
    target_id: DbId
    enabled: bool = True
    # The message an agent run starts with; required for agents.
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
    last_status: TriggerStatus | None
    last_error: str | None
    last_run_id: int | None
    created_at: datetime
    updated_at: datetime


class PreviewIn(BaseModel):
    cron: str = Field(max_length=255)
    timezone: str = Field("UTC", max_length=64)


class PreviewOut(BaseModel):
    description: str
    next_runs: list[datetime]


def trigger_out(trigger: Trigger) -> TriggerOut:
    target = trigger.agent if trigger.target_kind == TriggerTargetKind.agent else trigger.workflow
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
        last_status=trigger.last_status,
        last_error=trigger.last_error,
        last_run_id=trigger.last_run_id,
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
    if body.target_kind == TriggerTargetKind.agent and not body.input.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A schedule for an agent needs a message to start its run with.",
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


def load(db: Session, trigger_id: int) -> Trigger:
    db.expire_all()
    return db.scalars(
        select(Trigger)
        .where(Trigger.id == trigger_id)
        .options(selectinload(Trigger.agent), selectinload(Trigger.workflow))
    ).one()


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
    """The user's triggers, the next to fire first; disabled ones last."""
    query = (
        select(Trigger)
        .where(Trigger.owner_id == user.id)
        .options(selectinload(Trigger.agent), selectinload(Trigger.workflow))
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
    """Replace the trigger's settings; its next run is worked out again from now."""
    trigger = get_owned_trigger(db, user.id, trigger_id)
    apply(db, user.id, trigger, body)
    db.commit()
    return trigger_out(load(db, trigger.id))


@router.delete("/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trigger(trigger_id: DbId, user: CurrentUser, db: DB) -> None:
    db.delete(get_owned_trigger(db, user.id, trigger_id))
    db.commit()
