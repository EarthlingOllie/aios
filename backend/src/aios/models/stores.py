"""The store registry: each organisation's data store, and the run history export's outbox
(until phase 1 removes it)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    # The revision of the tree's `history` branch it's at: null until a member first switches
    # the run history export on, which creates the `history` schema.
    history_revision: Mapped[str | None] = mapped_column(String(64))
    # When the run history export last caught up (copied in whatever runs were pending).
    history_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When embedding the exported messages last caught up (embedded every one waiting); null
    # while it hasn't since it was switched on, or since it last failed.
    history_embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why embedding last failed, and when it's tried again (unless the store's export runs
    # first, after a turn); both cleared once it catches up.
    history_embed_error: Mapped[str | None] = mapped_column(Text)
    history_embed_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Copy the organisation's agent runs and workflow runs into the store's `history` schema
    # (`aios.datastores.history`, on the Organisation page).
    export_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    # Embed the exported messages too, so agents can search past runs by meaning. Only while
    # the export is on: switching it off switches this off.
    embed_run_history: Mapped[bool] = mapped_column(server_default=text("false"))
    # Why provisioning, or the last migration, failed.
    error: Mapped[str | None] = mapped_column(Text)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # What it uses, as last measured (`aios.datastores.quota`, on a schedule): the data its
    # tables hold and its documents' files' bytes. NULL until first measured.
    db_bytes: Mapped[int | None] = mapped_column(BigInteger)
    files_bytes: Mapped[int | None] = mapped_column(BigInteger)
    usage_measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "owner_organisation_id IS NOT NULL OR status IN ('deleting', 'deleted')",
            name="ck_data_stores_owner_organisation",
        ),
    )


class HistoryExportKind(enum.StrEnum):
    agent_run = "agent_run"
    workflow_run = "workflow_run"


class HistoryExport(Base):
    """The run history export's outbox: a run (by kind and platform id) to copy into the data
    store, or to delete from it if it's gone (`aios.datastores.history`).

    One row per run and store. It's written in the same transaction as the run's own update,
    and written again (`exported_at`, `error` and `retry_at` cleared) each time the run
    changes, so it's pending while `exported_at` is null. A run that failed to export keeps its
    `error`, and is set aside until `retry_at` (or until it changes again, if that's sooner), so
    it doesn't hold up the runs after it.
    """

    __tablename__ = "history_exports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    data_store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_stores.id", ondelete="CASCADE")
    )
    kind: Mapped[HistoryExportKind] = mapped_column(
        Enum(HistoryExportKind, name="history_export_kind")
    )
    # Not a foreign key: the row outlives a deleted run, to delete its copy.
    source_id: Mapped[int] = mapped_column(BigInteger)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("data_store_id", "kind", "source_id"),
        # What the export drains.
        Index(
            "ix_history_exports_pending",
            "data_store_id",
            "enqueued_at",
            postgresql_where=text("exported_at IS NULL"),
        ),
    )
