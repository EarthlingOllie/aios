"""Connectors: connections to external systems (Slack, Google Drive, GitHub, and the test
provider), one per provider per organisation.

**Connecting** is an OAuth-style flow. An admin starts it (`start_state`): the state is signed,
bound to their session (a nonce kept in it), the organisation, the provider and an expiry
(`connector_state_ttl_seconds`). The provider sends the browser back to one callback with a code
and the state (`read_state`): a bad or expired state is refused (`BadState`, 400), and one from
another session or person too (`WrongSession`, 403). The code is exchanged for a grant, kept as the
connector's credentials (`keep_credential`).

**Credentials** live in the platform database (`aios.models.ConnectorCredential`), encrypted with
a key derived from CONNECTOR_CREDENTIALS_KEY, apart from the stores' passwords' key. Nothing but
`credential` reads them back, and nothing returns, logs or traces them. A final authorisation
failure marks the connector as needing reconnection (`needs_reconnecting`); reconnecting runs the
flow again and carries on syncing from the stored cursor.

**What syncs** is the connector's scope: channels, folders or repositories an admin picks from the
provider's options (`aios.source_providers.ScopeOption`). Picking something restricted at the
source needs an acknowledgement (`RESTRICTED_WARNING`). Saving the scope makes the connector's
source (of kind `connector`), which syncs; narrowing it makes the next sync a full one, which
removes what's no longer in scope.

**Removing** a connector stops its syncs at once, deletes its credentials and revokes them at the
provider where it can; then (`finish_removal`) its content is either deleted or kept: file items'
documents become uploads, and message items become Markdown documents in Uploads, grouped as the
provider says (`aios.source_providers.Provider.keep_groups`).
"""

import base64
import contextlib
import hashlib
import io
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from aios import source_providers, sync
from aios.config import get_settings
from aios.datastores import files, store_session
from aios.datastores.models import (
    Connector,
    ConnectorStatus,
    Document,
    Source,
    SourceStatus,
    SyncedItem,
    SyncedItemKind,
)
from aios.db import SessionLocal
from aios.models import ConnectorCredential, CredentialKind
from aios.source_providers import Grant, NeedsReconnecting
from aios.sources import uploads_source_id

log = logging.getLogger(__name__)

RESTRICTED_WARNING = (
    "Some of what you picked is restricted at its source. Once it's synced, everyone in the "
    "organisation will be able to search it."
)

# --- The signed state ----------------------------------------------------------------------------

# Where the admin's session keeps the nonce its connect flows' states are bound to.
SESSION_NONCE = "connector_nonce"
_STATE_SALT = "aios.connectors.state"


class BadState(Exception):
    """A connect flow's state that isn't one, or has expired (400)."""


class WrongSession(Exception):
    """A connect flow's state started by another session or person (403)."""


@dataclass(frozen=True)
class State:
    user_id: int
    organisation_id: int
    provider: str
    # The connector being reconnected; None for a new connection.
    connector_id: int | None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret.get_secret_value(), _STATE_SALT)


def start_state(session: dict, state: State) -> str:
    """A signed state for a connect flow, bound to the session it's started in (whose nonce is
    made here if it has none)."""
    nonce = session.get(SESSION_NONCE)
    if not nonce:
        nonce = session[SESSION_NONCE] = secrets.token_urlsafe(16)
    return _serializer().dumps(
        {
            "u": state.user_id,
            "o": state.organisation_id,
            "p": state.provider,
            "c": state.connector_id,
            "n": hashlib.sha256(nonce.encode()).hexdigest(),
        }
    )


def read_state(token: str, session: dict, user_id: int) -> State:
    """The state a provider sent back, checked: `BadState` if it isn't one or has expired,
    `WrongSession` if it was started in another session or by someone else."""
    try:
        raw = _serializer().loads(token, max_age=get_settings().connector_state_ttl_seconds)
    except SignatureExpired as exc:
        raise BadState("This connection took too long. Start connecting again.") from exc
    except BadSignature as exc:
        raise BadState("This isn't a connection aios started.") from exc
    nonce = session.get(SESSION_NONCE) or ""
    if raw["u"] != user_id or raw["n"] != hashlib.sha256(nonce.encode()).hexdigest():
        raise WrongSession("This connection was started by someone else, or in another browser.")
    return State(raw["u"], raw["o"], raw["p"], raw["c"])


# --- Credentials ---------------------------------------------------------------------------------


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


def needs_reconnecting(store: Session, store_id: uuid.UUID, connector_id: int) -> None:
    """A final authorisation failure: the connector needs reconnecting (in `store`'s
    transaction), and its credentials are marked revoked."""
    store.execute(
        update(Connector)
        .where(Connector.id == connector_id, Connector.status == ConnectorStatus.active)
        .values(status=ConnectorStatus.needs_reauth)
    )
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


# --- Removing ------------------------------------------------------------------------------------

# Where a removed connector's source keeps the admin's keep-or-delete choice until it's done.
KEEP = "keep_documents"


def removals_due(store: Session) -> list[int]:
    """The store's connectors being removed whose removal hasn't finished in a while (its process
    stopped): finished by the run service's sweep."""
    stale = func.now() - timedelta(seconds=get_settings().sync_stale_seconds)
    return list(
        store.scalars(
            select(Connector.id).where(
                Connector.status == ConnectorStatus.removing, Connector.updated_at < stale
            )
        )
    )


def finish_removal(store_id: uuid.UUID, connector_id: int) -> None:
    """Delete or keep (as its source says) a removed connector's content, then the connector.
    Safe to run again: what's done stays done."""
    kept: list[int] = []
    with store_session(store_id, "rw") as store:
        connector = store.get(Connector, connector_id)
        if connector is None or connector.status != ConnectorStatus.removing:
            return
        source = store.scalar(select(Source).where(Source.connector_id == connector_id))
        if source is not None:
            if (source.settings or {}).get(KEEP):
                kept = _keep(store, store_id, source, connector.provider)
            sync.remove_items(store, source.id)
            store.execute(delete(Source).where(Source.id == source.id))
        store.execute(delete(Connector).where(Connector.id == connector_id))
        store.commit()
    sync.queue_ingest(store_id, kept)


def _keep(store: Session, store_id: uuid.UUID, source: Source, provider: str) -> list[int]:
    """Make the source's content Uploads documents: its file items' documents move to Uploads,
    and its message items become Markdown documents there. The new documents' ids, to ingest."""
    uploads = uploads_source_id(store)
    store.execute(
        update(Document)
        .where(Document.source_id == source.id)
        .values(source_id=uploads, synced_item_id=None)
    )
    messages = store.scalars(
        select(SyncedItem).where(
            SyncedItem.source_id == source.id, SyncedItem.kind == SyncedItemKind.message
        )
    ).all()
    found = source_providers.provider(provider)
    if not messages or found is None or found.keep_groups is None:
        return []
    made = []
    for kept in found.keep_groups(messages):
        doc_id = store.scalar(select(func.nextval(sync.DOCUMENT_IDS)))
        key = files.document_key(store_id, doc_id)
        stored = files.put(key, io.BytesIO(kept.markdown.encode()), "text/markdown")
        store.add(
            Document(
                id=doc_id,
                filename=kept.filename,
                content_type="text/markdown",
                s3_key=key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                source_id=uploads,
            )
        )
        made.append(doc_id)
    store.flush()
    return made


def stop(store: Session, connector: Connector, keep: bool) -> None:
    """Stop the connector at once (in `store`'s transaction): it and its source are being
    removed, which no sync starts on, and the keep-or-delete choice is kept for
    `finish_removal`."""
    connector.status = ConnectorStatus.removing
    source = store.scalar(
        select(Source).where(Source.connector_id == connector.id).with_for_update()
    )
    if source is not None:
        source.status = SourceStatus.removing
        source.settings = {**(source.settings or {}), KEEP: keep}
