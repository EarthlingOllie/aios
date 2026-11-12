"""Webhook triggers' public endpoint: `POST /api/webhooks/{token}`, called by systems outside,
signed with the trigger's secret (`aios.webhooks`), with no session and no organisation in the
path: the token leads to the trigger's store (`WebhookToken`).

In order, before anything is written:

1. **404** for a token that leads nowhere, or to a trigger that's disabled, deleted or no
   longer a webhook's.
2. **429** past `webhook_rate` calls to the token (with `Retry-After`).
3. **415** for a body that isn't JSON by its content type; **413** past
   `webhook_body_limit_bytes`; **400** for one that doesn't parse.
4. **401** for a timestamp more than `webhook_tolerance_seconds` off, or a bad signature.
5. **202** for a delivery id (`X-Aios-Delivery`, optional) the trigger has accepted in the last
   `webhook_dedupe_hours`: nothing more is written.
6. **422** for a body that doesn't fit the target workflow's input schema, as its run's input
   (the body, or the trigger's template rendered with it).
7. **Jev** checks the body (`aios.jev`): flagged is a **422**, audited (`webhook.flagged`);
   unchecked a **503** with `Retry-After`. Either way the delivery id isn't recorded, so the
   caller's retry is checked again.

Then the delivery id and a webhook received event (its delivery id and body, for this trigger
alone) are written in one transaction, and the answer is **202** with the delivery id: the
dispatcher fires the trigger on its own time (`aios.dispatcher`).
"""

import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from aios import audit, dispatcher, jev, outbox, ratelimits, webhooks
from aios.api.workflows import run_input
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import Trigger, WebhookDelivery, Workflow
from aios.db import SessionLocal
from aios.models import EventType, TriggerKind, TriggerTargetKind, WebhookToken

router = APIRouter(prefix="/api")

SOURCE = "webhook"
FLAGGED = "Jev flagged this body as a likely prompt injection, so nothing fired."
UNCHECKED = "The body couldn't be checked for prompt injection just now. Try again later."
MAX_DELIVERY_ID = 200


@dataclass(frozen=True)
class _Found:
    data_store_id: uuid.UUID
    trigger_id: int
    secret: str
    template: str
    # The target workflow's input schema, if it's a workflow's and has one.
    schema: Any


def _token(token_hash: str) -> WebhookToken | None:
    with SessionLocal() as db:
        return db.get(WebhookToken, token_hash)


def _trigger(index: WebhookToken, token_hash: str) -> _Found | None:
    """The enabled webhook trigger the token leads to, from its store."""
    with store_session(index.data_store_id) as store:
        trigger = store.scalar(
            select(Trigger).where(
                Trigger.id == index.trigger_id,
                Trigger.webhook_token_hash == token_hash,
                Trigger.kind == TriggerKind.event,
                Trigger.event_type == EventType.webhook_received,
                Trigger.enabled.is_(True),
            )
        )
        if trigger is None or trigger.webhook_secret is None:
            return None
        schema = None
        if trigger.target_kind == TriggerTargetKind.workflow:
            workflow = store.get(Workflow, trigger.workflow_id)
            if workflow is not None and workflow.current_version is not None:
                schema = workflow.current_version.input_schema
        return _Found(
            index.data_store_id,
            trigger.id,
            webhooks.decrypt(trigger.webhook_secret),
            trigger.input,
            schema,
        )


def _seen(found: _Found, delivery_id: str) -> bool:
    cutoff = timedelta(hours=get_settings().webhook_dedupe_hours)
    with store_session(found.data_store_id) as store:
        return (
            store.scalar(
                select(WebhookDelivery.delivery_id).where(
                    WebhookDelivery.trigger_id == found.trigger_id,
                    WebhookDelivery.delivery_id == delivery_id,
                    WebhookDelivery.received_at > func.now() - cutoff,
                )
            )
            is not None
        )


def _flagged(found: _Found, delivery_id: str, reason: str | None) -> None:
    with store_session(found.data_store_id) as store:
        audit.record(
            store,
            audit.SYSTEM,
            "webhook.flagged",
            "trigger",
            found.trigger_id,
            {"delivery_id": delivery_id, "reason": reason or ""},
        )
        store.commit()


def _received(found: _Found, delivery_id: str, body: Any) -> bool:
    """Record the delivery and write its event, in one transaction; False if the delivery id
    was recorded meanwhile (a concurrent call with it), when nothing is written."""
    with store_session(found.data_store_id) as store:
        # One the window has passed is replaced.
        stored = store.execute(
            insert(WebhookDelivery)
            .values(trigger_id=found.trigger_id, delivery_id=delivery_id)
            .on_conflict_do_update(
                index_elements=[WebhookDelivery.trigger_id, WebhookDelivery.delivery_id],
                set_={"received_at": func.now()},
                where=WebhookDelivery.received_at
                <= func.now() - timedelta(hours=get_settings().webhook_dedupe_hours),
            )
            .returning(WebhookDelivery.delivery_id)
        ).first()
        if stored is None:
            store.rollback()
            return False
        outbox.emit(
            store,
            EventType.webhook_received,
            {"trigger_id": found.trigger_id, "delivery_id": delivery_id, "body": body},
        )
        store.commit()
    return True


def _check_schema(found: _Found, body: Any) -> None:
    """A 422 if the run's input, from this body, wouldn't fit the target workflow's schema."""
    if found.schema is None:
        return
    document = {
        "type": EventType.webhook_received.value,
        "payload": {"trigger_id": found.trigger_id, "delivery_id": "", "body": body},
    }
    run_input(found.schema, dispatcher.render_input(found.template, document))


async def _body(request: Request) -> bytes:
    """The raw body, at most `webhook_body_limit_bytes`: a 413 past it."""
    limit = get_settings().webhook_body_limit_bytes
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The body is too large.")
    return bytes(body)


def _not_json(constant: str) -> Any:
    raise ValueError(f"{constant} isn't JSON")


def _is_json(content_type: str | None) -> bool:
    media = (content_type or "").split(";")[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


@router.post("/webhooks/{token}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(token: str, request: Request) -> JSONResponse:
    settings = get_settings()
    token_hash = webhooks.token_hash(token)
    index = await run_in_threadpool(_token, token_hash)
    found = await run_in_threadpool(_trigger, index, token_hash) if index is not None else None
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await run_in_threadpool(ratelimits.throttle, "webhook", settings.webhook_rate, token_hash)
    if not _is_json(request.headers.get("content-type")):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The body must be JSON.")
    raw = await _body(request)

    stamp = request.headers.get(webhooks.TIMESTAMP_HEADER, "").strip()
    given = request.headers.get(webhooks.SIGNATURE_HEADER, "")
    if (
        not (stamp.isascii() and stamp.isdigit())
        or abs(time.time() - int(stamp)) > settings.webhook_tolerance_seconds
        or not webhooks.signed(found.secret, stamp, raw, given)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad signature or timestamp.")
    try:
        # NaN and Infinity aren't JSON, and can't be stored as it.
        body = json.loads(raw, parse_constant=_not_json)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The body isn't valid JSON.") from None

    delivery_id = request.headers.get(webhooks.DELIVERY_HEADER, "").strip()
    if len(delivery_id) > MAX_DELIVERY_ID:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"A delivery id is at most {MAX_DELIVERY_ID} characters."
        )
    if delivery_id and await run_in_threadpool(_seen, found, delivery_id):
        return _accepted(delivery_id)
    delivery_id = delivery_id or str(uuid.uuid4())

    await run_in_threadpool(_check_schema, found, body)
    try:
        judgement = await jev.check(raw.decode("utf-8", "replace"), SOURCE)
    except jev.JevUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            UNCHECKED,
            headers={"Retry-After": str(settings.webhook_retry_after_seconds)},
        ) from None
    if judgement.flagged:
        await run_in_threadpool(_flagged, found, delivery_id, judgement.reason)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": FLAGGED, "reason": judgement.reason or ""},
        )
    await run_in_threadpool(_received, found, delivery_id, body)
    return _accepted(delivery_id)


def _accepted(delivery_id: str) -> JSONResponse:
    return JSONResponse({"delivery_id": delivery_id}, status_code=status.HTTP_202_ACCEPTED)
