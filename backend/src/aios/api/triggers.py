"""Triggers: what starts agents and workflows without a user in the loop.

Schedules are the only kind so far. Each targets one agent (a new run of its current version,
starting with the trigger's `input` as the user's message) or one workflow (a new run of its
current version, with `input` as the run's input). `aios.scheduler` fires them.

A trigger is its organisation's, like its target: every member sees, changes and deletes every
one, and each says which member made it and which last changed it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aios import cron, stored
from aios.api.provenance import Provenance, UserOut, made_by, people, provenance
from aios.api.skills import lock_live
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotFound,
    DataStoreNotReady,
    organisation_store,
)
from aios.datastores.models import Agent, AgentRun, Workflow, WorkflowRun
from aios.db import DB, DbId
from aios.models import (
    Organisation,
    Trigger,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
    WorkflowRunStatus,
)
from aios.permissions import Action, allow
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


def last_workflow_runs(organisation_id: int, triggers: Sequence[Trigger]) -> LastRuns:
    """How the workflow runs these triggers of the organisation's last started are going, from
    its store. Those whose worker died are failed first, so they aren't shown running for ever
    (as `api.workflows.get_workflow_run` does). None while the store can't be read: the
    triggers then show their own outcomes."""
    ids = [t.last_workflow_run_id for t in triggers if t.last_workflow_run_id is not None]
    if not ids:
        return {}
    try:
        with organisation_store(organisation_id) as store:
            fail_stopped_runs(store, WorkflowRun.id.in_(ids))
            rows = store.execute(
                select(WorkflowRun.id, WorkflowRun.status, WorkflowRun.error).where(
                    WorkflowRun.id.in_(ids)
                )
            ).all()
    except (DataStoreNotFound, DataStoreNotReady, DataStoreBusy):
        return {}
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
    organisation_id: int, triggers: Sequence[Trigger]
) -> dict[tuple[TriggerTargetKind, int], str]:
    """The names of the organisation's triggers' targets, from its store, by (kind, id)."""
    found: dict[tuple[TriggerTargetKind, int], str] = {}
    for kind, model in ((TriggerTargetKind.agent, Agent), (TriggerTargetKind.workflow, Workflow)):
        ids = [target_id(t) for t in triggers if t.target_kind == kind]
        for i, name in stored.names(organisation_id, model, ids).items():
            found[kind, i] = name
    return found


def trigger_out(
    db: Session,
    trigger: Trigger,
    known: dict[int, UserOut] | None = None,
    names: dict[tuple[TriggerTargetKind, int], str] | None = None,
    runs: LastRuns | None = None,
) -> TriggerOut:
    """The trigger, with who made and last changed it from `known` (`provenance.people`), its
    target's name from `names` (`target_names`) and its last workflow run from `runs`
    (`last_workflow_runs`), else read now."""
    if names is None:
        names = target_names(trigger.organisation_id, [trigger])
    if runs is None:
        runs = last_workflow_runs(trigger.organisation_id, [trigger])
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


def get_org_trigger(db: Session, org: Organisation, trigger_id: int) -> Trigger:
    """The organisation's trigger; a 404 otherwise, as if it didn't exist."""
    trigger = db.get(Trigger, trigger_id)
    if trigger is None or trigger.organisation_id != org.id:
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


def apply(db: Session, store: Session, user_id: int, trigger: Trigger, body: TriggerIn) -> None:
    """Set the trigger from the request (made by `user_id`), checked, and schedule its next run.
    Its target must be one of the organisation's: in its store, `store`, whose share lock on the
    target is held until the caller ends `store`'s transaction, after committing `db`'s."""
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
    # Share-locked until the store's transaction ends, after the trigger is committed, so a
    # concurrent archive of the target (which pauses its triggers) either waits and then pauses
    # this one, or wins and is seen here.
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
    db.expire_all()
    return db.scalars(select(Trigger).where(Trigger.id == trigger_id)).one()


def disable_triggers(db: Session, condition, user_id: int) -> None:
    """Turn off the triggers matching `condition`, as pausing each would (and as the member
    `user_id` changing them): none fires again until it's turned back on, and a firing claimed
    but not started is dropped. Lock their targets first (see `aios.scheduler._claimed`)."""
    db.execute(
        update(Trigger)
        .where(condition)
        .values(enabled=False, next_run_at=None, updated_by=user_id)
        .execution_options(synchronize_session=False)
    )
    cancel_unstarted_firing(db, condition)


@router.get("/triggers", response_model=list[TriggerOut], dependencies=[allow(Action.read)])
def list_triggers(
    org: CurrentOrganisation,
    db: DB,
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
    query = (
        select(Trigger)
        .where(Trigger.organisation_id == org.id)
        .order_by(Trigger.next_run_at.asc().nulls_last(), Trigger.id)
    )
    if target_kind == TriggerTargetKind.agent:
        query = query.where(Trigger.agent_id == target_id)
    elif target_kind == TriggerTargetKind.workflow:
        query = query.where(Trigger.workflow_id == target_id)
    triggers = db.scalars(query).all()
    known = people(db, org.id, [i for t in triggers for i in (t.created_by, t.updated_by)])
    names = target_names(org.id, triggers)
    runs = last_workflow_runs(org.id, triggers)
    return [trigger_out(db, t, known, names, runs) for t in triggers]


@router.post(
    "/triggers",
    response_model=TriggerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[allow(Action.build)],
)
def create_trigger(
    body: TriggerIn, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    trigger = Trigger(**made_by(org.id, user.id))
    apply(db, store, user.id, trigger, body)
    db.add(trigger)
    db.commit()
    store.commit()
    return trigger_out(db, load(db, trigger.id))


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
def get_trigger(trigger_id: DbId, org: CurrentOrganisation, db: DB):
    return trigger_out(db, load(db, get_org_trigger(db, org, trigger_id).id))


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
    trigger = get_org_trigger(db, org, trigger_id)
    apply(db, store, user.id, trigger, body)
    cancel_unstarted_firing(db, Trigger.id == trigger.id)
    db.commit()
    store.commit()
    return trigger_out(db, load(db, trigger.id))


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[allow(Action.build)],
)
def delete_trigger(trigger_id: DbId, org: CurrentOrganisation, db: DB, store: CurrentStore) -> None:
    db.delete(get_org_trigger(db, org, trigger_id))
    db.commit()
    # The runs it started (in the store, naming it by id) no longer name it; not a change to an
    # agent run itself, so its `updated_at` stays (listings sort by it).
    store.execute(
        update(AgentRun)
        .where(AgentRun.trigger_id == trigger_id)
        .values(trigger_id=None, updated_at=AgentRun.updated_at)
    )
    store.execute(
        update(WorkflowRun).where(WorkflowRun.trigger_id == trigger_id).values(trigger_id=None)
    )
    store.commit()
