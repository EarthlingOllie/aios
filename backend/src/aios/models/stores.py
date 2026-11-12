"""The store registry: each organisation's data store."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aios.models.base import Base


class DataStoreStatus(enum.StrEnum):
    # Its database is being created (`aios.datastores.provision`); nothing can use it yet.
    provisioning = "provisioning"
    ready = "ready"
    # Provisioning gave up; re-running it (`aios.datastores.provision.retry_unfinished` does, every
    # few minutes, as does the deploy's `migrate_all`) carries on.
    failed = "failed"
    # Its organisation was deleted (an empty one, on accepting an invitation): the provisioning
    # worker's sweep drops its database, roles and files (`provision.delete_deleting`) ...
    deleting = "deleting"
    # ... and then it's this: the row is kept, as a record of the store.
    deleted = "deleted"


class DataStore(Base):
    """Where an organisation's data lives: a database of its own (see `aios.datastores`).

    It has an id of its own, rather than being its organisation's, so a store could change
    hands (it did, from users to organisations) without renaming anything named after it: its
    database and roles are named after this id, never its organisation's. Its documents and
    chunks are in its database (`aios.datastores.models`), not here.
    """

    __tablename__ = "data_stores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # The organisation whose store it is: one each. Null only once it's `deleting` (its
    # organisation was deleted) or `deleted`.
    owner_organisation_id: Mapped[int | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[DataStoreStatus] = mapped_column(
        Enum(DataStoreStatus, name="data_store_status"),
        default=DataStoreStatus.provisioning,
        server_default=DataStoreStatus.provisioning.value,
    )
    # Where its database is, if not on the platform database's instance (where every store is,
    # for now, and NULL says so): kept so a store can later move to another instance.
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    # `ds_<id as hex>`, and its roles `<role_prefix>_owner`, `_rw` and `_ro`.
    dbname: Mapped[str] = mapped_column(String(63), unique=True)
    role_prefix: Mapped[str] = mapped_column(String(56), unique=True)
    # The login roles' passwords, encrypted (`aios.datastores.credentials`). Never logged, and
    # never returned by the API.
    rw_password: Mapped[str | None] = mapped_column(Text)
    ro_password: Mapped[str | None] = mapped_column(Text)
    # The store tree's Alembic revision its database is at (`aios.datastores.migrations`).
    schema_revision: Mapped[str | None] = mapped_column(String(64))
    # The revision of the tree's `history` branch it's at: null unless its organisation once
    # switched the (former) run history export on; its head drops the export's copy.
    history_revision: Mapped[str | None] = mapped_column(String(64))
    # Why provisioning, or the last migration, failed.
    error: Mapped[str | None] = mapped_column(Text)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # What it uses, as last measured (`aios.datastores.quota`, on a schedule): the data its
    # tables hold and its documents' files' bytes. NULL until first measured.
    db_bytes: Mapped[int | None] = mapped_column(BigInteger)
    files_bytes: Mapped[int | None] = mapped_column(BigInteger)
    usage_measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When its outbox last got events the dispatcher hasn't seen (`aios.events`): stamped once
    # the store has committed them, cleared as the dispatcher claims the store. Null: none.
    events_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_data_stores_events_pending_at",
            "events_pending_at",
            postgresql_where=text("events_pending_at IS NOT NULL"),
        ),
        CheckConstraint(
            "owner_organisation_id IS NOT NULL OR status IN ('deleting', 'deleted')",
            name="ck_data_stores_owner_organisation",
        ),
    )


class AgentStoreRole(Base):
    """An agent's own login role in its organisation's store (`aios.datastores.agent_roles`),
    which `write_data` writes as: named from the store and the agent, with its password
    generated here, kept encrypted (`aios.datastores.credentials`) and given to Postgres only as
    a SCRAM verifier. Kept while the agent is archived (unarchiving restores the same login);
    `dropped_at` once the role is gone (its store was deleted)."""

    __tablename__ = "agent_store_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_stores.id", ondelete="CASCADE")
    )
    # The agent, by its id in the store (`aios.agents.id` there): ids repeat across stores.
    agent_id: Mapped[int] = mapped_column(Integer)
    role_name: Mapped[str] = mapped_column(String(63), unique=True)
    password: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dropped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_agent_store_roles_store_agent", "data_store_id", "agent_id", unique=True),
    )
