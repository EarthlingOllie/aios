"""The tables every data store has, in its `aios` schema (the store tree, `migrations/`, makes
them). Sessions on them come from `store_session`, on the store's own database: they're never
in the platform database (`aios.models`), and a store's rows are all its own, so nothing here
says whose they are.
"""

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024

SCHEMA = "aios"


class StoreBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class SchemaHistory(StoreBase):
    """The log of changes to the store's schema: approved schema change requests append to it
    as they're applied."""

    __tablename__ = "schema_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # The schema change request it applied (in the platform database), if any.
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(Text)
    # The statements that ran.
    sql: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmbeddingColumn(StoreBase):
    """A `vector(EMBEDDING_DIM)` column of a table in `data`, and the text column in the same
    table it's the embedding of: `write_data` fills it. Schema changes add a row as they add
    such a column; only the owner writes it (`provision.BOOKKEEPING`)."""

    __tablename__ = "embedding_columns"
    __table_args__ = {
        "comment": "Columns in data that write_data fills with embeddings of a text column"
    }

    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    column_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_column: Mapped[str] = mapped_column(Text)


class DroppedObject(StoreBase):
    """A table or column in `data` that an approved drop renamed out of the way, until it's
    restored or purged (`aios.datastores.dropped`). Only the owner writes it
    (`provision.BOOKKEEPING`)."""

    __tablename__ = "dropped_objects"
    __table_args__ = {
        "comment": "Tables and columns in data that were dropped, until restored or purged"
    }

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # A dropped table: its name before it was dropped. A dropped column: the table it's in, as
    # that's named now (a dropped table's new name, if the table was dropped after it).
    table_name: Mapped[str] = mapped_column(Text)
    # A dropped column: its name before it was dropped. None for a table.
    column_name: Mapped[str | None] = mapped_column(Text)
    # What it was renamed to.
    dropped_name: Mapped[str] = mapped_column(Text)
    # A column that was NOT NULL (dropping it lifts that, so new rows can leave it out).
    not_null: Mapped[bool] = mapped_column(Boolean, server_default=false())
    dropped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Why the last restore the owner asked for was refused, if it was.
    restore_error: Mapped[str | None] = mapped_column(Text)


class DocumentStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class Document(StoreBase):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    # Where the upload is, in S3 (`aios.datastores.files.document_key`), its size and its
    # SHA-256 (hex).
    s3_key: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Ingest task deliveries so far; capped so a doc that kills the worker isn't retried forever.
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Chunk.ordinal",
    )


class Chunk(StoreBase):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    # Raw chunk text (shown to users / the model) and the heading-enriched text that was embedded.
    text: Mapped[str] = mapped_column(Text)
    contextualized: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', contextualized)", persisted=True)
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_document_id", "document_id"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )
