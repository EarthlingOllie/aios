"""The tables every data store has, in its `aios` schema, and the run history export's, in its
`history` schema once its owner first switches the export on (the store tree, `migrations/`,
makes them). Sessions on them come from `store_session`, on the store's own database: they're never
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
    PrimaryKeyConstraint,
    String,
    Text,
    false,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024

SCHEMA = "aios"
# The run history export's tables (`aios.datastores.history`).
HISTORY_SCHEMA = "history"
# The exported messages worth embedding, and searching: what the user and the assistant said
# (not tool results, which are the tools' own, nor the empty text of a turn of only tool calls).
EMBEDDABLE = "role IN ('user', 'assistant') AND btrim(text) <> ''"


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


# The run history export: a copy of the owner's agent and workflow runs, keyed by the platform's
# ids. The platform database stays the source of truth; these are only ever written by the
# export.


class HistoryAgentRun(StoreBase):
    __tablename__ = "agent_runs"
    __table_args__ = {
        "schema": HISTORY_SCHEMA,
        "comment": "A copy of an agent run (one conversation with an agent), from the platform.",
    }

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The run's id on the platform."
    )
    agent_id: Mapped[int] = mapped_column(BigInteger)
    agent_name: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[int] = mapped_column(Integer, comment="The agent version it ran.")
    model: Mapped[str | None] = mapped_column(
        Text, comment="The version's model; null means the deployment's default."
    )
    title: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(BigInteger, comment="The user who ran it.")
    trigger_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The schedule that started it, if one did."
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="The workflow run whose agent node it was, if any."
    )
    message_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="When its last turn ended."
    )
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoryMessage(StoreBase):
    __tablename__ = "messages"

    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey(f"{HISTORY_SCHEMA}.agent_runs.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer, comment="Its place in the run, from 0.")
    message_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, comment="user, assistant, tool or system.")
    text: Mapped[str] = mapped_column(Text, comment="Its text, without tool calls.")
    tool_calls: Mapped[list | None] = mapped_column(
        JSONB, comment="An assistant message's tool calls: [{id, name, args}]."
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        Text, comment="A tool message's call, in its run's tool_calls."
    )
    tool_name: Mapped[str | None] = mapped_column(Text)
    is_error: Mapped[bool | None] = mapped_column(Boolean, comment="A tool message that failed.")
    usage: Mapped[dict | None] = mapped_column(
        JSONB, comment="An assistant message's token usage: {input_tokens, output_tokens, ...}."
    )
    model: Mapped[str | None] = mapped_column(Text, comment="The model that wrote it.")
    # For searching past runs (`aios.search`): the text's embedding, while the owner has
    # embedding on, and its full-text vector, always.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM),
        comment="Its text's embedding, for search: a user's or assistant's message with text, "
        "once embedded (only while embedding is on).",
    )
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        comment="Its text's full-text search vector.",
    )

    __table_args__ = (
        PrimaryKeyConstraint("agent_run_id", "position"),
        Index(
            "ix_messages_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
        # The messages waiting to be embedded (`EMBEDDABLE`), for the embedding to find.
        Index(
            "ix_messages_unembedded",
            "agent_run_id",
            "position",
            postgresql_where=sql_text(f"embedding IS NULL AND {EMBEDDABLE}"),
        ),
        {
            "schema": HISTORY_SCHEMA,
            "comment": "The messages of the agent runs in history.agent_runs, in order.",
        },
    )


class HistoryWorkflowRun(StoreBase):
    __tablename__ = "workflow_runs"
    __table_args__ = {
        "schema": HISTORY_SCHEMA,
        "comment": "A copy of a workflow run, from the platform.",
    }

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The run's id on the platform."
    )
    workflow_id: Mapped[int] = mapped_column(BigInteger)
    workflow_name: Mapped[str] = mapped_column(Text)
    workflow_version: Mapped[int] = mapped_column(Integer, comment="The workflow version it ran.")
    user_id: Mapped[int] = mapped_column(BigInteger, comment="The user who ran it.")
    # Text, not an enum: a status the platform adds later is copied as it is.
    status: Mapped[str] = mapped_column(
        Text, comment="queued, running, succeeded or failed, as on the platform when exported."
    )
    input: Mapped[str] = mapped_column(Text, comment="What it was given.")
    output: Mapped[str | None] = mapped_column(Text, comment="Its final nodes' outputs, once done.")
    error: Mapped[str | None] = mapped_column(Text, comment="Why it failed.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoryWorkflowRunStep(StoreBase):
    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        Index("ix_workflow_run_steps_workflow_run_id", "workflow_run_id"),
        {
            "schema": HISTORY_SCHEMA,
            "comment": "The steps of the workflow runs in history.workflow_runs: a node, at any "
            "depth.",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, comment="The step's id on the platform."
    )
    workflow_run_id: Mapped[int] = mapped_column(
        ForeignKey(f"{HISTORY_SCHEMA}.workflow_runs.id", ondelete="CASCADE")
    )
    path: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        comment="Node keys from the run's workflow down to the node: {review,critic} is "
        "node critic of the subworkflow at node review.",
    )
    kind: Mapped[str] = mapped_column(Text, comment="agent or workflow.")
    status: Mapped[str] = mapped_column(
        Text, comment="running, succeeded or failed, as on the platform when exported."
    )
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # Not a foreign key: agent runs and workflow runs are exported apart, in either order.
    agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="An agent node's conversation: history.agent_runs.id."
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
