"""Connectors' credentials (`aios.connectors`), in the platform database
(`aios.models.ConnectorCredential`), encrypted with a key derived from CONNECTOR_CREDENTIALS_KEY,
apart from the stores' passwords' key. Nothing but `credential` reads them back, and nothing
returns, logs or traces them.

Apart from `aios.connectors` because these open platform sessions, and that module works on
the stores' documents: only the stores' seam (`aios.datastores`) may do both.
"""

import base64
import contextlib
import hashlib
import logging
import uuid
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from aios import source_providers
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import ConnectorCredential, CredentialKind
from aios.source_providers import Grant, NeedsReconnecting

log = logging.getLogger("aios.connectors")


@lru_cache
def _fernet(key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def _cipher() -> Fernet:
    return _fernet(get_settings().connector_credentials_key.get_secret_value())


def keep_credential(
    db: Session,
    organisation_id: int,
    store_id: uuid.UUID,
    connector_id: int,
    provider: str,
    grant: Grant,
    user_id: int,
) -> None:
    """Keep the grant as the connector's credentials (replacing any it had), in `db`'s
    transaction."""
    row = db.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.data_store_id == store_id,
            ConnectorCredential.connector_id == connector_id,
        )
    )
    if row is None:
        row = ConnectorCredential(
            organisation_id=organisation_id, data_store_id=store_id, connector_id=connector_id
        )
        db.add(row)
    row.provider = provider
    row.kind = CredentialKind(grant.kind)
    row.secret = _cipher().encrypt(grant.secret.encode()).decode()
    row.scopes = grant.scopes
    row.expires_at = grant.expires_at
    row.external_account = grant.external_account
    row.created_by = user_id
    row.revoked_at = None


def credential(store_id: uuid.UUID, connector_id: int) -> str:
    """The connector's secret: the one way it's read. Raises `NeedsReconnecting` if it has none
    usable (removed, revoked, or unreadable with today's key)."""
    with SessionLocal() as db:
        row = db.scalar(
            select(ConnectorCredential).where(
                ConnectorCredential.data_store_id == store_id,
                ConnectorCredential.connector_id == connector_id,
            )
        )
        if row is None or row.revoked_at is not None:
            raise NeedsReconnecting("The connector has no working credentials.")
        try:
            return _cipher().decrypt(row.secret.encode()).decode()
        except Exception as exc:
            raise NeedsReconnecting("The connector's credentials can't be read.") from exc


def revoke_credential(store_id: uuid.UUID, connector_id: int) -> None:
    """Mark the connector's credentials revoked (a final authorisation failure: it needs
    reconnecting, `aios.connectors.needs_reconnecting`)."""
    with SessionLocal() as db:
        db.execute(
            update(ConnectorCredential)
            .where(
                ConnectorCredential.data_store_id == store_id,
                ConnectorCredential.connector_id == connector_id,
            )
            .values(revoked_at=func.now())
        )
        db.commit()


def drop_credential(store_id: uuid.UUID, connector_id: int, provider: str) -> None:
    """Delete the connector's credentials, revoking them at the provider first where it can (a
    provider that can't be reached doesn't stop the removal)."""
    secret = None
    with contextlib.suppress(NeedsReconnecting):
        secret = credential(store_id, connector_id)
    found = source_providers.provider(provider)
    if secret is not None and found is not None and found.revoke is not None:
        try:
            found.revoke(secret)
        except Exception:
            log.warning("couldn't revoke connector %s's access at %s", connector_id, provider)
    with SessionLocal() as db:
        db.execute(
            delete(ConnectorCredential).where(
                ConnectorCredential.data_store_id == store_id,
                ConnectorCredential.connector_id == connector_id,
            )
        )
        db.commit()
