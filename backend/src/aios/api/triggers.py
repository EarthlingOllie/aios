"""Triggers: what starts agents and workflows without a user in the loop.

Each targets one agent (a new run of its current version) or one workflow (a new run of its
current version). Two kinds:

- **Schedules** start the agent's run with the trigger's `input` as the user's message, or the
  workflow's with `input` as its input. `aios.scheduler` fires them.
- **Event triggers** fire on an event in the store's outbox of their `event_type`, that passes
  their `filter` (`aios.outbox`). The agent's run gets `input` (if any) and the event; the
  workflow's input is `input` as a template rendered with the event, or the event as JSON.
  `aios.dispatcher` fires them, under the loop rules; a firing they refuse is the trigger's
  last outcome (`refused`, or `rate_limited`). With a debounce window, the matches of that
  many seconds gather into one firing.
- **Chat message triggers** listen to one chat (`filter.chat_id`), any of the organisation's,
  whether or not whoever saves the trigger is in it; saving one adds its agent or workflow to
  that chat, as delivery does to the chat it delivers to.
- **Webhook triggers** fire on calls to their own public endpoint (`aios.api.webhooks`). Made,
  they're given its URL and signing secret, the secret only then and when it's rotated
  (`rotate_webhook_secret`), which changes the URL too (`aios.webhooks`).

A trigger is its organisation's, like its target, and is in its store beside it: every member
sees, changes and deletes every one, and each says which member made it and which last changed
it. Once the store has committed a change, the platform's schedule due index is written
(`aios.schedules`), which is how the scheduler finds what's due.

**Deliver to.** A trigger may name a chat its runs' results are delivered to (`deliver_to`):
any chat of the organisation that isn't archived, whether or not whoever sets it is in it (an
archived one is refused with a 409). Setting it adds the trigger's agent or workflow to the chat,
and records who chose it. When a run ends its result is handed back and posted there, or a short
failure message is (`aios.invocations`). If the chat has been archived since, the run still runs
but nothing is delivered: the trigger shows why (`delivery_problem`), and whoever chose the chat
is notified.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import audit, cron, outbox, schedules, streams, webhooks
from aios.api.provenance import Provenance, UserOut, built_by, people, provenance
from aios.api.skills import lock_live
from aios.auth import CurrentOrganisation, CurrentStore, CurrentUser
from aios.datastores import store_id_of
from aios.datastores.models import (
    Agent,
    Chat,
    ChatMember,
    Invocation,
    Trigger,
    Workflow,
    WorkflowRun,
)
from aios.db import DB, DbId
from aios.models import (
    ChatMemberKind,
    EventType,
    TriggerKind,
    TriggerStatus,
    TriggerTargetKind,
    WorkflowRunStatus,
)
from aios.permissions import Action, allow

router = APIRouter(prefix="/api")

PREVIEW_RUNS = 5


class TriggerIn(BaseModel):
    kind: Literal[TriggerKind.schedule, TriggerKind.event] = TriggerKind.schedule
    target_kind: TriggerTargetKind
    target_id: DbId
    enabled: bool = True
    # Schedules: the message an agent run starts with, or a workflow run's input; required.
    # Event triggers: what an agent's run is told besides the event, or the template a
    # workflow run's input is rendered from (`aios.dispatcher.render_input`); optional.
    input: str = Field("", max_length=100_000)
    # Schedules only.
    cron: str | None = Field(None, max_length=255)
    timezone: str = Field("UTC", max_length=64)
    # Event triggers only: the event type, the filter (by `outbox.FILTERS`' fields), and
    # whether it may fire on events its own target caused.
    event_type: EventType | None = None
    filter: dict = Field(default_factory=dict)
    fire_on_own_events: bool = False
    # Event triggers only: gather the matches of this many seconds into one firing; null fires
    # on each.
    debounce_seconds: int | None = Field(None, ge=1, le=24 * 60 * 60)
    # The chat its runs' results are delivered to; null for none.
    deliver_to: DbId | None = None


class DestinationOut(BaseModel):
    """The chat a trigger delivers to."""

    id: int
    name: str
    archived: bool


class TriggerOut(Provenance):
    id: int
    kind: TriggerKind
    target_kind: TriggerTargetKind
    target_id: int
    target_name: str
    enabled: bool
    input: str
    # Schedules: the cron expression; else null. When it fires, in plain English: "At 09:00 on
    # Monday", or "When a document is ingested".
    cron: str | None
    description: str
    timezone: str | None
    # Event triggers: what fires them; else null, empty and false.
    event_type: EventType | None
    filter: dict
    fire_on_own_events: bool
    debounce_seconds: int | None
    # Webhook triggers: the endpoint to call; else null. Its signing secret only in the answer
    # that made or rotated it.
    webhook_url: str | None
    webhook_secret: str | None = None
    # Why the platform turned it off, if it did.
    disabled_reason: str | None
    next_run_at: datetime | None
    last_fired_at: datetime | None
    # For a workflow, its last run's: a worker runs it after the trigger has fired. An event
    # trigger's firing the loop rules refused is `refused` (`rate_limited` past its rate), with
    # why as the error.
    last_status: TriggerStatus | None
    last_error: str | None
    # The agent run (agents) or workflow run (workflows) it last started.
    last_run_id: int | None
    last_workflow_run_id: int | None
    # The chat its results are delivered to, and who chose it; null for none.
    deliver_to: DestinationOut | None
    deliver_to_set_by: UserOut | None
    # Why its last run's result wasn't delivered to that chat (it was archived); null if it was.
    delivery_problem: str | None
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


# The chats triggers deliver to (id, name, archived), and why each one's last run's result
# wasn't delivered, by trigger id.
Deliveries = tuple[dict[int, DestinationOut], dict[int, str]]


def deliveries(store: Session, triggers: Sequence[Trigger]) -> Deliveries:
    """The chats these triggers deliver to, and the delivery problems of their last runs to
    those chats, from their store."""
    chat_ids = {t.destination_chat_id for t in triggers if t.destination_chat_id is not None}
    chats: dict[int, DestinationOut] = {}
    if chat_ids:
        for i, name, archived_at in store.execute(
            select(Chat.id, Chat.name, Chat.archived_at).where(Chat.id.in_(chat_ids))
        ):
            chats[i] = DestinationOut(id=i, name=name, archived=archived_at is not None)
    problems: dict[int, str] = {}
    by_id = {t.id: t for t in triggers if t.destination_chat_id is not None}
    if by_id:
        for trigger_id, chat_id, error in store.execute(
            select(Invocation.trigger_id, Invocation.delivery_chat_id, Invocation.delivery_error)
            .where(Invocation.trigger_id.in_(list(by_id)), Invocation.parent_id.is_(None))
            .distinct(Invocation.trigger_id)
            .order_by(Invocation.trigger_id, Invocation.id.desc())
        ):
            # Only a problem with the chat it delivers to now: choosing another clears it.
            if error and chat_id == by_id[trigger_id].destination_chat_id:
                problems[trigger_id] = error
    return chats, problems


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
    delivered: Deliveries | None = None,
) -> TriggerOut:
    """The organisation's trigger (in its store, `store`), with who made and last changed it
    from `known` (`provenance.people`), its target's name from `names` (`target_names`) and its
    last workflow run from `runs` (`last_workflow_runs`), else read now."""
    if known is None:
        known = people(
            db,
            organisation_id,
            [trigger.created_by, trigger.updated_by, trigger.destination_set_by],
        )
    if delivered is None:
        delivered = deliveries(store, [trigger])
    chats, problems = delivered
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
        description=cron.describe(trigger.cron)
        if trigger.cron
        else outbox.DESCRIPTIONS[trigger.event_type],
        timezone=trigger.timezone,
        event_type=trigger.event_type,
        filter=trigger.filter or {},
        fire_on_own_events=trigger.fire_on_own_events,
        debounce_seconds=trigger.debounce_seconds,
        webhook_url=webhooks.url(webhooks.decrypt(trigger.webhook_secret))
        if trigger.webhook_secret
        else None,
        disabled_reason=trigger.disabled_reason,
        next_run_at=trigger.next_run_at,
        last_fired_at=trigger.last_fired_at,
        last_status=last_status,
        last_error=last_error,
        last_run_id=trigger.last_run_id,
        last_workflow_run_id=trigger.last_workflow_run_id,
        deliver_to=chats.get(trigger.destination_chat_id),
        deliver_to_set_by=known.get(trigger.destination_set_by)
        if trigger.destination_chat_id is not None and trigger.destination_set_by is not None
        else None,
        delivery_problem=problems.get(trigger.id),
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


@dataclass
class Applied:
    """What saving a trigger left to do: a webhook trigger's new secret, to show once (its
    token's hash to index before the store commits), and the token hash it no longer has, to
    forget after."""

    secret: str | None = None
    dropped_hash: str | None = None


def apply(store: Session, user_id: int, trigger: Trigger, body: TriggerIn) -> Applied:
    """Set the trigger from the request (made by `user_id`), checked, and schedule its next run.
    Its target must be one of the organisation's, in its store, `store`, whose share lock on the
    target is held until the caller ends `store`'s transaction."""
    listened = (trigger.filter or {}).get("chat_id") if trigger.event_type else None
    if body.kind == TriggerKind.event:
        expr = tz = None
        event_type, event_filter = checked_event(body)
    else:
        if body.cron is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A schedule needs a cron.")
        expr, tz = checked_schedule(body.cron, body.timezone)
        event_type, event_filter = None, {}
    if body.kind == TriggerKind.schedule and not body.input.strip():
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
    if (
        event_type == EventType.webhook_received
        and body.debounce_seconds is not None
        and model is Workflow
        and target.current_version is not None
        and target.current_version.input_schema
    ):
        # A debounced run is given the batch, not a body, so it can't meet the schema each
        # call's body is checked against.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A webhook trigger for {target.name}, which has an input schema, can't be debounced.",
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
    trigger.event_type = event_type
    trigger.filter = event_filter
    trigger.fire_on_own_events = body.fire_on_own_events and event_type is not None
    trigger.debounce_seconds = body.debounce_seconds if event_type is not None else None
    trigger.disabled_reason = None
    trigger.updated_by = user_id
    trigger.next_run_at = (
        cron.next_run(expr, tz, datetime.now(UTC)) if body.enabled and expr is not None else None
    )
    deliver_to(store, user_id, trigger, body.deliver_to)
    if event_type == EventType.chat_message:
        listen_to(store, user_id, trigger, event_filter["chat_id"], listened)
    applied = Applied()
    if event_type == EventType.webhook_received:
        if trigger.webhook_secret is None:
            applied.secret = new_webhook_secret(trigger)
    elif trigger.webhook_token_hash is not None:
        applied.dropped_hash = trigger.webhook_token_hash
        trigger.webhook_token_hash = trigger.webhook_secret = None
    return applied


def new_webhook_secret(trigger: Trigger) -> str:
    """Give the webhook trigger a new secret (and so a new token): the secret, to show once."""
    secret = webhooks.new_secret()
    trigger.webhook_secret = webhooks.encrypt(secret)
    trigger.webhook_token_hash = webhooks.token_hash(webhooks.token(secret))
    return secret


def listen_to(
    store: Session, user_id: int, trigger: Trigger, chat_id: int, listened: int | None
) -> None:
    """A chat message trigger listens to the chat: any of the organisation's that isn't archived
    (a 422 for one it hasn't, a 409 for an archived one, unless it `listened` to it already),
    whether or not `user_id` is in it. Its agent or workflow is added to the chat."""
    chat = store.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown chat")
    if chat.archived_at is not None:
        if listened == chat_id:
            return
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{chat.name} is archived, so there's nothing to listen to."
        )
    _include(store, user_id, chat, trigger)


ARCHIVED_DESTINATION = (
    "{name} is archived, so results can't be delivered there. Choose another chat."
)


def deliver_to(store: Session, user_id: int, trigger: Trigger, chat_id: int | None) -> None:
    """Set the chat the trigger's results are delivered to (None: none), as `user_id` chose it:
    a 422 for a chat the organisation hasn't, a 409 for an archived one. Its agent or workflow
    is added to the chat if it isn't in it. Keeping the chat it had keeps who chose it."""
    if chat_id is None:
        trigger.destination_chat_id = None
        trigger.destination_set_by = None
        return
    chat = store.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown chat")
    if chat_id != trigger.destination_chat_id:
        if chat.archived_at is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, ARCHIVED_DESTINATION.format(name=chat.name)
            )
        trigger.destination_chat_id = chat.id
        trigger.destination_set_by = user_id
    if chat.archived_at is None:
        _include(store, user_id, chat, trigger)


def _include(store: Session, user_id: int, chat: Chat, trigger: Trigger) -> None:
    """Add the trigger's agent or workflow to the chat, unless it's in it already."""
    kind = ChatMemberKind(trigger.target_kind.value)
    column = ChatMember.agent_id if kind == ChatMemberKind.agent else ChatMember.workflow_id
    found = store.scalar(
        select(ChatMember.id).where(ChatMember.chat_id == chat.id, column == target_id(trigger))
    )
    if found is not None:
        return
    store.execute(
        insert(ChatMember)
        .values(
            chat_id=chat.id,
            kind=kind,
            added_by=user_id,
            **{f"{kind.value}_id": target_id(trigger)},
        )
        .on_conflict_do_nothing()
    )
    streams.members_changed(store, chat.id)


def checked_event(body: TriggerIn) -> tuple[EventType, dict]:
    """An event trigger's event type and filter (normalised); a 422 if there's no event type, or
    one triggers can't be made for yet, or the filter has an unknown field or a bad value."""
    if body.event_type is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "An event trigger needs an event type."
        )
    if body.event_type not in outbox.TRIGGERABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Triggers can't fire on {body.event_type.value} events yet.",
        )
    try:
        return body.event_type, outbox.checked_filter(body.event_type, body.filter)
    except outbox.BadFilter as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


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
    """What an audit entry says of a trigger: what it starts, when, and whether it's on."""
    if trigger.kind == TriggerKind.event:
        return {
            "target_kind": trigger.target_kind.value,
            "target_id": trigger.agent_id or trigger.workflow_id,
            "event_type": trigger.event_type.value,
            "filter": trigger.filter,
            "fire_on_own_events": trigger.fire_on_own_events,
            "debounce_seconds": trigger.debounce_seconds,
            "enabled": trigger.enabled,
            "deliver_to": trigger.destination_chat_id,
        }
    return {
        "target_kind": trigger.target_kind.value,
        "target_id": trigger.agent_id or trigger.workflow_id,
        "cron": trigger.cron,
        "timezone": trigger.timezone,
        "enabled": trigger.enabled,
        "deliver_to": trigger.destination_chat_id,
    }


def _audited(trigger: Trigger) -> str:
    """What the audit log calls the trigger: a schedule, or an event trigger."""
    return "schedule" if trigger.kind == TriggerKind.schedule else "trigger"


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
    known = people(
        db,
        org.id,
        [i for t in triggers for i in (t.created_by, t.updated_by, t.destination_set_by)],
    )
    names = target_names(store, triggers)
    runs = last_workflow_runs(store, triggers)
    delivered = deliveries(store, triggers)
    return [trigger_out(db, store, org.id, t, known, names, runs, delivered) for t in triggers]


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
    applied = apply(store, user.id, trigger, body)
    store.add(trigger)
    audit.configured(store, user.id, _audited(trigger), trigger, "created", **_described(trigger))
    return _committed(db, store, org.id, trigger, applied)


def _committed(
    db: Session, store: Session, organisation_id: int, trigger: Trigger, applied: Applied
) -> TriggerOut:
    """Commit the saved trigger, with the indexes around it: a new webhook token's before the
    store commits it, the schedule due index and a dropped token's after. Its new webhook
    secret, if it has one, is in the answer."""
    store.flush()
    if applied.secret is not None:
        webhooks.index(store_id_of(store), organisation_id, trigger.id, trigger.webhook_token_hash)
    store.commit()
    reindex(store, [trigger])
    if applied.dropped_hash is not None:
        webhooks.unindex([applied.dropped_hash])
    out = trigger_out(db, store, organisation_id, load(store, trigger.id))
    out.webhook_secret = applied.secret
    return out


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
    applied = apply(store, user.id, trigger, body)
    store.flush()
    cancel_unstarted_firing(store, Trigger.id == trigger.id)
    audit.configured(store, user.id, _audited(trigger), trigger, "updated", **_described(trigger))
    return _committed(db, store, org.id, trigger, applied)


@router.post(
    "/triggers/{trigger_id}/webhook-secret",
    response_model=TriggerOut,
    dependencies=[allow(Action.build)],
)
def rotate_webhook_secret(
    trigger_id: DbId, user: CurrentUser, org: CurrentOrganisation, db: DB, store: CurrentStore
):
    """Replace a webhook trigger's signing secret, and so its URL: the old ones stop working at
    once. The new secret is in the answer, and only there. A 409 for a trigger that isn't a
    webhook's."""
    trigger = get_org_trigger(store, trigger_id, lock=True)
    if trigger.event_type != EventType.webhook_received or trigger.webhook_secret is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This trigger has no webhook.")
    applied = Applied(dropped_hash=trigger.webhook_token_hash)
    applied.secret = new_webhook_secret(trigger)
    trigger.updated_by = user.id
    audit.configured(store, user.id, "trigger", trigger, "webhook_secret_rotated")
    return _committed(db, store, org.id, trigger, applied)


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
    audit.configured(store, user.id, _audited(trigger), trigger, "deleted", **_described(trigger))
    token_hash = trigger.webhook_token_hash
    store.delete(trigger)
    store.commit()
    unindex(store, [trigger_id])
    if token_hash is not None:
        webhooks.unindex([token_hash])
