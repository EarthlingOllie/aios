"""Webhook triggers' secrets, tokens and signatures (the endpoint is `aios.api.webhooks`).

- **The secret.** Each webhook trigger has a random signing secret, kept encrypted on the
  trigger (`credentials.encrypt`), shown once when it's made or rotated.
- **The token** in its endpoint's URL is derived from the secret (`token`), so the URL can be
  shown again while only the token's SHA-256 is kept (`token_hash`): on the trigger, and in the
  platform's `WebhookToken` index, which leads the endpoint to the trigger's store. Rotating the
  secret changes the URL too.
- **Signatures.** A caller sends the time (`X-Aios-Timestamp`, Unix seconds) and
  `X-Aios-Signature: sha256=<hex>`, the HMAC-SHA256 of `<timestamp>.<raw body>` keyed by the
  secret (`signature`).
"""

import hashlib
import hmac
import secrets
import uuid

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from aios.config import get_settings
from aios.datastores import credentials
from aios.db import SessionLocal
from aios.models import WebhookToken

TIMESTAMP_HEADER = "X-Aios-Timestamp"
SIGNATURE_HEADER = "X-Aios-Signature"
DELIVERY_HEADER = "X-Aios-Delivery"
PATH = "/api/webhooks/"


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def token(secret: str) -> str:
    """The token in the URL of the trigger whose secret this is."""
    return hmac.new(secret.encode(), b"aios webhook token", hashlib.sha256).hexdigest()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def url(secret: str) -> str:
    """The trigger's endpoint, as callers outside call it."""
    return get_settings().public_url.rstrip("/") + PATH + token(secret)


def encrypt(secret: str) -> str:
    return credentials.encrypt(secret)


def decrypt(stored: str) -> str:
    return credentials.decrypt(stored)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The `X-Aios-Signature` value for a body sent at `timestamp`."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def signed(secret: str, timestamp: str, body: bytes, given: str) -> bool:
    # compare_digest takes only ASCII str; a header may carry any latin-1 character.
    given = given.strip()
    return given.isascii() and hmac.compare_digest(signature(secret, timestamp, body), given)


def index(data_store_id: uuid.UUID, organisation_id: int, trigger_id: int, hashed: str) -> None:
    """Lead the token with this hash to the trigger; committed at once, before the store commits
    the trigger's hash."""
    with SessionLocal() as db:
        db.execute(
            insert(WebhookToken)
            .values(
                token_hash=hashed,
                data_store_id=data_store_id,
                trigger_id=trigger_id,
                organisation_id=organisation_id,
            )
            .on_conflict_do_nothing()
        )
        db.commit()


def unindex(hashes: list[str]) -> None:
    """Forget these tokens, once the store has committed dropping them."""
    if not hashes:
        return
    with SessionLocal() as db:
        db.execute(delete(WebhookToken).where(WebhookToken.token_hash.in_(hashes)))
        db.commit()
