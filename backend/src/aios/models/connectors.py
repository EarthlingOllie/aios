"""Connectors' credentials (`aios.connectors`): kept here, in the platform database, never in a
store, so nothing that queries a store (agents among it) can reach them."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class CredentialKind(enum.StrEnum):
    # An OAuth access (and refresh) token: Slack, Google Drive, the test provider.
    oauth = "oauth"
    # A GitHub App installation: the secret is the installation id; tokens are minted from it.
    github_installation = "github_installation"


class ConnectorCredential(Base):
    """One connector's credentials: the secret encrypted with a key derived from
    CONNECTOR_CREDENTIALS_KEY (apart from the stores' passwords' key). Read only through
    `aios.connectors.credential`; never returned by the API, logged, traced or shown to a model.
    Deleted when its connector is removed, and with its organisation."""

    __tablename__ = "connector_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_stores.id", ondelete="CASCADE")
    )
    # The connector, by its id in the store (`aios.connectors.id` there): ids repeat across stores.
    connector_id: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    kind: Mapped[CredentialKind] = mapped_column(Enum(CredentialKind, name="credential_kind"))
    # Encrypted (Fernet): the token, or the installation id.
    secret: Mapped[str] = mapped_column(Text)
    # What the provider granted, as it says (not secret).
    scopes: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The workspace or installation at the provider (not secret).
    external_account: Mapped[str | None] = mapped_column(Text)
    # The admin who connected it, by platform user id.
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # When the provider said it's no longer good (a final authorisation failure).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "data_store_id", "connector_id", name="uq_connector_credentials_store_connector"
        ),
    )
