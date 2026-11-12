"""Webhook and email sources: sources other systems push to, rather than ones that sync.

**Webhook sources** are webhooks that keep their deliveries: the one webhook concept (webhook
triggers, `aios.webhooks`, are the other way to use it). A webhook source has a signing secret,
kept encrypted on the source and shown once, and a token derived from it in its URL, leading to
it through the platform's webhook token index (`aios.models.WebhookToken`); rotating the secret
changes both. Its endpoint is the webhook triggers' (`aios.api.webhooks`), with the same
signature, timestamp, size and rate checks, and Jev's check before anything is stored:

- a delivery Jev finds clean is stored as a searchable message item (named by its delivery id)
  and fires the webhook received event, for the event triggers listening to the source
  (`store_delivery`);
- a flagged one is stored as flagged (not searchable), fires nothing and answers 422;
- one Jev can't check answers 503 and isn't stored, so the caller retries.

A delivery id already stored isn't stored again. Webhook items are never reconciled away: the
source never syncs (`aios.sync.PUSHED`).

**Email sources** have an unguessable address on the deployment's inbound mail domain
(`inbound_mail_domain`), shown once; only its SHA-256 is kept, on the source and in the
platform's inbound address index, so regenerating it cuts the old one off. The receiving
service posts each message (`aios.api.inbound_mail`; `receive_mail` here). A message is
accepted only from an allowed sender (none by default: nothing is accepted) and only when it
passed sender authentication (SPF or DKIM, as the receiving service reports); anything else is
dropped and counted on a sync record. An accepted message becomes a message item, threaded
under the first message of its thread by its reply headers, and its attachments (each within
`email_attachment_max_bytes`, else dropped and counted) file items that become documents, all
checked by Jev as every synced item is (`aios.sync.push`). Without an inbound mail domain email
sources are unavailable (`email_unavailable`).
"""

import base64
import binascii
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from aios import audit, outbox, search_index, sync, webhooks
from aios.config import get_settings
from aios.datastores import store_session
from aios.datastores.models import (
    Source,
    SourceKind,
    SourceStatus,
    SyncedItem,
    SyncedItemKind,
)
from aios.db import SessionLocal
from aios.jev import Verdict
from aios.models import EventType, InboundAddress
from aios.source_providers import Content, Item

EMAIL_UNAVAILABLE = (
    "Email sources need an inbound mail service, which this deployment doesn't have. They're on "
    "the roadmap for deployments like this one."
)
NO_SENDERS = "Dropped a message: no senders are allowed yet."
NOT_ALLOWED = "Dropped a message from a sender who isn't allowed."
UNAUTHENTICATED = "Dropped a message that failed sender authentication (SPF and DKIM)."
TOO_BIG = "Dropped an attachment past the size limit."


def email_unavailable() -> str | None:
    """Why email sources can't be added here (None if they can)."""
    return None if get_settings().inbound_mail_domain else EMAIL_UNAVAILABLE


# --- Webhook sources ---------------------------------------------------------------------------


def new_webhook_secret(source: Source) -> str:
    """Give the webhook source a new secret, and so a new token: the secret, to show once."""
    secret = webhooks.new_secret()
    source.secret = webhooks.encrypt(secret)
    source.token_hash = webhooks.token_hash(webhooks.token(secret))
    return secret


@dataclass(frozen=True)
class FoundSource:
    data_store_id: uuid.UUID
    source_id: int
    secret: str


def webhook_source(data_store_id: uuid.UUID, source_id: int, token_hash: str) -> FoundSource | None:
    """The active webhook source the token leads to, from its store."""
    with store_session(data_store_id) as store:
        source = store.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.kind == SourceKind.webhook,
                Source.token_hash == token_hash,
                Source.status == SourceStatus.active,
            )
        )
        if source is None or source.secret is None:
            return None
        return FoundSource(data_store_id, source_id, webhooks.decrypt(source.secret))


def delivery_verdict(found: FoundSource, delivery_id: str) -> Verdict | None:
    """The verdict on the delivery already stored by this id; None if there's none."""
    with store_session(found.data_store_id) as store:
        return store.scalar(
            select(SyncedItem.verdict).where(
                SyncedItem.source_id == found.source_id, SyncedItem.external_id == delivery_id
            )
        )


def store_delivery(
    found: FoundSource,
    delivery_id: str,
    raw: str,
    body: Any,
    verdict: Verdict,
    reason: str | None = None,
    version: str | None = None,
) -> bool:
    """Store a delivery Jev has judged (clean or flagged) as a message item, with its search
    entries, and, if clean, its webhook received event; flagged, an audit entry. One
    transaction. False if a delivery by this id was stored meanwhile, when nothing is."""
    with store_session(found.data_store_id, "rw") as store:
        text, truncated = sync._cap(raw)
        item_id = store.scalar(
            insert(SyncedItem)
            .values(
                source_id=found.source_id,
                external_id=delivery_id,
                kind=SyncedItemKind.message,
                title=f"Delivery {delivery_id}",
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                text=text,
                truncated=truncated,
                verdict=verdict,
                verdict_reason=reason,
                verdict_version=version,
                checked_at=func.now(),
                source_updated_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=[SyncedItem.source_id, SyncedItem.external_id])
            .returning(SyncedItem.id)
        )
        if item_id is None:
            store.rollback()
            return False
        search_index.index_synced_item(store, store.get_one(SyncedItem, item_id))
        if verdict == Verdict.clean:
            outbox.emit(
                store,
                EventType.webhook_received,
                {
                    "source_id": found.source_id,
                    "delivery_id": delivery_id,
                    "item_id": item_id,
                    "body": body,
                },
            )
        else:
            audit.record(
                store,
                audit.SYSTEM,
                "webhook.flagged",
                "source",
                found.source_id,
                {"delivery_id": delivery_id, "reason": reason or ""},
            )
        sync.record_push(
            store,
            found.source_id,
            sync.Counts(added=1),
            1 if verdict == Verdict.awaiting_check else 0,
        )
    return True


# --- Email sources -----------------------------------------------------------------------------


def address_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def new_address(source: Source) -> str:
    """Give the email source a new address (cutting the old one off once its index entry
    goes): the address, to show once."""
    address = f"in-{secrets.token_hex(16)}@{get_settings().inbound_mail_domain}"
    source.token_hash = address_hash(address)
    return address


def index_address(
    data_store_id: uuid.UUID, organisation_id: int, source_id: int, hashed: str
) -> None:
    """Lead the address with this hash to the source; committed at once, before the store
    commits the source's hash."""
    with SessionLocal() as db:
        db.execute(
            insert(InboundAddress)
            .values(
                address_hash=hashed,
                data_store_id=data_store_id,
                source_id=source_id,
                organisation_id=organisation_id,
            )
            .on_conflict_do_nothing()
        )
        db.commit()


def unindex_address(hashes: list[str]) -> None:
    if not hashes:
        return
    with SessionLocal() as db:
        db.execute(delete(InboundAddress).where(InboundAddress.address_hash.in_(hashes)))
        db.commit()


def checked_senders(given: list[str]) -> list[str]:
    """The allowed senders as kept: each an address or a domain (`@example.com` or
    `example.com`), trimmed and lower-cased, unique, in order. Raises `ValueError`."""
    out = []
    for entry in given:
        value = entry.strip().lower()
        if not value:
            continue
        local, at, domain = value.rpartition("@")
        domain = domain if at else value
        if (
            any(c.isspace() for c in value)
            or value.count("@") > 1
            or "." not in domain
            or domain.startswith(".")
            or domain.endswith(".")
        ):
            raise ValueError(f"{entry.strip()!r} isn't an email address or a domain.")
        if at and not local:
            value = "@" + domain
        elif not at:
            value = "@" + value
        out.append(value)
    return list(dict.fromkeys(out))


def sender_allowed(sender: str, allowed: list[str]) -> bool:
    sender = sender.strip().lower()
    domain = sender.rpartition("@")[2]
    return any(sender == a or (a.startswith("@") and domain == a[1:]) for a in allowed)


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str | None
    data: bytes


@dataclass(frozen=True)
class Mail:
    """A message as the receiving service reports it."""

    recipients: list[str]
    sender: str
    subject: str = ""
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    text: str = ""
    # The receiving service's sender authentication results ("pass", "fail", ...).
    spf: str | None = None
    dkim: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


def decode_attachment(content: str) -> bytes:
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("An attachment's content isn't base64.") from None


def _message_id(value: str | None) -> str | None:
    value = (value or "").strip().strip("<>").strip()
    return value or None


def _authenticated(mail: Mail) -> bool:
    return any((r or "").strip().lower() == "pass" for r in (mail.spf, mail.dkim))


@dataclass(frozen=True)
class _Found:
    data_store_id: uuid.UUID
    source_id: int
    allowed: list[str]


def _email_source(index: InboundAddress) -> _Found | None:
    with store_session(index.data_store_id) as store:
        source = store.scalar(
            select(Source).where(
                Source.id == index.source_id,
                Source.kind == SourceKind.email,
                Source.token_hash == index.address_hash,
                Source.status == SourceStatus.active,
            )
        )
        if source is None:
            return None
        return _Found(
            index.data_store_id, source.id, list((source.settings or {}).get("allowed_senders", []))
        )


def _thread_root(found: _Found, refs: list[str]) -> str | None:
    """The external id of the first message of the thread these references (nearest last)
    lead to, among the source's items."""
    if not refs:
        return None
    with store_session(found.data_store_id) as store:
        rows = {
            r.external_id: r
            for r in store.execute(
                select(SyncedItem.id, SyncedItem.external_id, SyncedItem.parent_id).where(
                    SyncedItem.source_id == found.source_id,
                    SyncedItem.kind == SyncedItemKind.message,
                    SyncedItem.external_id.in_(refs),
                )
            )
        }
        for ref in reversed(refs):
            row = rows.get(ref)
            if row is None:
                continue
            if row.parent_id is None:
                return row.external_id
            return store.scalar(
                select(SyncedItem.external_id).where(SyncedItem.id == row.parent_id)
            )
    return None


def _accept(found: _Found, mail: Mail) -> None:
    sender = parseaddr(mail.sender)[1]
    if not found.allowed:
        sync.push(found.data_store_id, found.source_id, [], {}, dropped=1, error=NO_SENDERS)
        return
    if not sender_allowed(sender, found.allowed):
        sync.push(found.data_store_id, found.source_id, [], {}, dropped=1, error=NOT_ALLOWED)
        return
    if not _authenticated(mail):
        sync.push(found.data_store_id, found.source_id, [], {}, dropped=1, error=UNAUTHENTICATED)
        return
    message_id = _message_id(mail.message_id) or f"aios-{uuid.uuid4()}"
    refs = [r for r in (_message_id(x) for x in mail.references) if r]
    replied = _message_id(mail.in_reply_to)
    if replied and replied not in refs:
        refs.append(replied)
    root = _thread_root(found, [r for r in refs if r != message_id])
    subject = mail.subject.strip() or "(no subject)"
    text = f"From: {mail.sender.strip()}\nSubject: {subject}\n\n{mail.text}"
    message = Item(
        external_id=message_id,
        kind=SyncedItemKind.message,
        title=subject,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        author_name=mail.sender.strip() or None,
        parent_external_id=root,
    )
    items, contents = [message], {message_id: Content(text=text)}
    dropped = 0
    cap = get_settings().email_attachment_max_bytes
    for n, attachment in enumerate(mail.attachments, start=1):
        if len(attachment.data) > cap:
            dropped += 1
            continue
        external_id = f"{message_id}#attachment-{n}"
        items.append(
            Item(
                external_id=external_id,
                kind=SyncedItemKind.file,
                title=attachment.filename or f"attachment-{n}",
                content_hash=hashlib.sha256(attachment.data).hexdigest(),
                author_name=mail.sender.strip() or None,
                parent_external_id=message_id,
                content_type=attachment.content_type,
            )
        )
        contents[external_id] = Content(data=attachment.data)
    sync.push(
        found.data_store_id,
        found.source_id,
        items,
        contents,
        dropped=dropped,
        error=TOO_BIG if dropped else None,
    )


def receive_mail(mail: Mail) -> int:
    """Take a message the receiving service posted: for each recipient that's an email
    source's address, accept it or drop it (see the module). How many sources it reached."""
    reached = 0
    hashes = {address_hash(a) for _, a in getaddresses(mail.recipients) if a}
    if not hashes:
        return 0
    with SessionLocal() as db:
        indexes = db.scalars(
            select(InboundAddress).where(InboundAddress.address_hash.in_(hashes))
        ).all()
        for index in indexes:
            db.expunge(index)
    for index in indexes:
        found = _email_source(index)
        if found is None:
            continue
        _accept(found, mail)
        reached += 1
    return reached
