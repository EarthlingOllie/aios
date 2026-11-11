"""The search index: one item per thing worth finding in an organisation's store (`SearchItem`),
searched by `aios.search`.

**Writing.** Each writer indexes in the same transaction as its own row, on the session it
writes with, so an item is there exactly when its source is (and goes with it: each kind's
source is a foreign key that cascades):

- ingest indexes a document's chunks (`index_chunks`);
- posting a chat message indexes it, and so does a run posting its result (`index_chat_message`);
- ending a turn indexes the user's and the assistant's text of it, not tool results
  (`index_turn`);
- the handoff indexes its brief (`index_brief`).

**Verdicts.** Each item carries its source's Jev verdict (`aios.verdicts`): a chunk its
document's, and the rest `derived` (what agents wrote, and people's chat messages until phase 2
checks them at posting). Search finds only clean, released and derived items.

**Embedding** is deferred: an item is written without one, and found by keyword until the
store's sweep (`embed_pending`, the `aios.embed_search_items` task, on a schedule) embeds it,
under a time budget per store per sweep. An item whose text is already embedded elsewhere in the
store (by its hash) takes that embedding instead of calling the embedding API again, and an item
written again with unchanged text keeps its embedding.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from sqlalchemy import bindparam, case, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aios import usage
from aios.config import get_settings
from aios.datastores.connections import store_session
from aios.datastores.models import (
    AgentRun,
    ChatMessage,
    Chunk,
    Document,
    Invocation,
    SearchItem,
    SearchItemKind,
)
from aios.embeddings import embed_documents
from aios.jev import Verdict
from aios.models import ChatMessageKind, UsagePurpose
from aios.usage import Charge

log = logging.getLogger(__name__)

# Items embedded per round trip to the embedding API (and to the store).
EMBED_BATCH = 128
# What's embedded of a long item: its start, so a batch stays inside the embedding API's limit
# on tokens per request.
EMBED_MAX_CHARS = 4000

# A run's messages worth finding: what the user and the assistant said, not tool results.
ROLES = {"human": "user", "ai": "assistant"}


def text_hash(searched: str) -> str:
    return hashlib.sha256(searched.encode()).hexdigest()


def _write(store: Session, conflict: list[str], **values: Any) -> None:
    """Write an item, or rewrite the one already there for its source (`conflict`): with new
    text, its embedding goes, for the sweep to make again; with the same, it's kept."""
    searched = values.get("search_text") or values["text"]
    values["text_hash"] = text_hash(searched)
    statement = insert(SearchItem).values(**values)
    table = SearchItem.__table__
    store.execute(
        statement.on_conflict_do_update(
            index_elements=conflict,
            set_={
                **{k: statement.excluded[k] for k in values if k not in conflict},
                "embedding": case(
                    (table.c.text_hash == statement.excluded.text_hash, table.c.embedding),
                    else_=None,
                ),
            },
        )
    )


def index_chunks(store: Session, document_id: int, verdict: Verdict | None = None) -> None:
    """Index the document's chunks (flushed), searched by their text with its headings, with
    `verdict`, else the document's own (`aios.verdicts`): ingest indexes them awaiting Jev's
    check."""
    if verdict is None:
        verdict = store.scalar(select(Document.verdict).where(Document.id == document_id))
    chunks = store.execute(
        select(Chunk.id, Chunk.text, Chunk.contextualized, Chunk.meta).where(
            Chunk.document_id == document_id
        )
    ).all()
    for chunk in chunks:
        meta = chunk.meta or {}
        _write(
            store,
            ["chunk_id"],
            kind=SearchItemKind.document_chunk,
            chunk_id=chunk.id,
            document_id=document_id,
            ref={"headings": meta.get("headings", []), "pages": meta.get("pages", [])},
            text=chunk.text,
            search_text=chunk.contextualized if chunk.contextualized != chunk.text else None,
            verdict=verdict,
        )


def index_chat_message(store: Session, message: ChatMessage) -> None:
    """Index a chat message with text (not a run's card, which follows its run). Flushes it."""
    if message.kind != ChatMessageKind.text or not message.body.strip():
        return
    store.flush()
    _write(
        store,
        ["message_id"],
        kind=SearchItemKind.chat_message,
        message_id=message.id,
        chat_id=message.chat_id,
        agent_id=message.author_agent_id,
        author_user_id=message.author_user_id,
        text=message.body,
        verdict=Verdict.derived,
    )


def index_brief(store: Session, invocation: Invocation) -> None:
    """Index the brief the handoff gave the invocation's run, if it said anything."""
    if not (invocation.brief or "").strip():
        return
    _write(
        store,
        ["invocation_id"],
        kind=SearchItemKind.brief,
        invocation_id=invocation.id,
        chat_id=invocation.chat_id,
        agent_id=invocation.agent_id,
        text=invocation.brief,
        verdict=Verdict.derived,
    )


def index_turn(
    store: Session, run_id: int, turn_id: int, messages: Sequence[BaseMessage], start: int
) -> None:
    """Index the turn's messages, those of the run's conversation (`messages`) from `start`:
    what the user and the assistant said, each by its place in the conversation. Those of an
    earlier turn are left as they are."""
    run = store.execute(
        select(AgentRun.agent_id, AgentRun.created_by).where(AgentRun.id == run_id)
    ).one_or_none()
    if run is None:
        return
    invocation = store.execute(
        select(Invocation.chat_id, Invocation.requested_by)
        .where(Invocation.agent_run_id == run_id)
        .order_by(Invocation.id)
        .limit(1)
    ).one_or_none()
    chat_id = invocation.chat_id if invocation else None
    requested_by = invocation.requested_by if invocation else run.created_by
    indexed = set(
        store.scalars(
            select(SearchItem.position).where(
                SearchItem.agent_run_id == run_id, SearchItem.position >= start
            )
        )
    )
    for position in range(max(start, 0), len(messages)):
        message = messages[position]
        role = ROLES.get(message.type)
        said = message.text if role else ""
        if not said.strip() or position in indexed:
            continue
        _write(
            store,
            ["turn_id", "position"],
            kind=SearchItemKind.run_message,
            turn_id=turn_id,
            position=position,
            agent_run_id=run_id,
            agent_id=run.agent_id,
            chat_id=chat_id,
            author_user_id=requested_by if role == "user" else None,
            ref={"role": role},
            text=said,
            verdict=Verdict.derived,
        )


# --- Embedding -------------------------------------------------------------------------------

REUSE_SQL = text(
    """
    UPDATE aios.search_items i SET embedding = e.embedding
    FROM (
        SELECT DISTINCT ON (text_hash) text_hash, embedding
        FROM aios.search_items
        WHERE embedding IS NOT NULL
          AND text_hash IN (SELECT text_hash FROM aios.search_items WHERE embedding IS NULL)
        ORDER BY text_hash, id
    ) e
    WHERE i.embedding IS NULL AND i.text_hash = e.text_hash
    """
)


def _reuse(data_store_id: uuid.UUID) -> int:
    """Give items whose text is embedded already (by another item) that embedding."""
    with store_session(data_store_id, "rw") as store:
        reused = store.execute(REUSE_SQL).rowcount
        store.commit()
    return reused


def _waiting(data_store_id: uuid.UUID) -> list[Any]:
    with store_session(data_store_id, "rw") as store:
        rows = store.execute(
            select(
                SearchItem.id,
                SearchItem.kind,
                SearchItem.text_hash,
                SearchItem.text,
                SearchItem.search_text,
            )
            .where(SearchItem.embedding.is_(None))
            .order_by(SearchItem.id)
            .limit(EMBED_BATCH)
        ).all()
        store.commit()
    return list(rows)


def _store_embeddings(data_store_id: uuid.UUID, rows: list[Any], vectors: list[list[float]]):
    table = SearchItem.__table__
    with store_session(data_store_id, "rw") as store:
        # Only if its text is still what was embedded: one rewritten meanwhile waits for the
        # next batch.
        store.execute(
            update(table)
            .where(
                table.c.id == bindparam("item_id"),
                table.c.text_hash == bindparam("hash"),
                table.c.embedding.is_(None),
            )
            .values(embedding=bindparam("vector")),
            [
                {"item_id": r.id, "hash": r.text_hash, "vector": v}
                for r, v in zip(rows, vectors, strict=True)
            ],
        )
        store.commit()


# What an embedding of each kind of item is for: a document's chunks are its ingest.
_PURPOSES = (
    (UsagePurpose.ingest, lambda kind: kind == SearchItemKind.document_chunk),
    (UsagePurpose.index_embedding, lambda kind: kind != SearchItemKind.document_chunk),
)


def embed_pending(data_store_id: uuid.UUID, budget: float | None = None) -> int:
    """Embed the store's items waiting for an embedding, a batch at a time, until none are left
    or `budget` seconds (`search_embed_budget_seconds`) have gone: what's left waits for the next
    sweep. No store session is held while the embedding API answers. Returns how many it
    embedded (or gave an existing embedding)."""
    if budget is None:
        budget = get_settings().search_embed_budget_seconds
    deadline = time.monotonic() + budget
    done = _reuse(data_store_id)
    while True:
        rows = _waiting(data_store_id)
        if not rows:
            break
        texts = [(r.search_text or r.text)[:EMBED_MAX_CHARS] for r in rows]
        # Under no charge, so it records nothing itself: its purposes are recorded here.
        with usage.charging(None):
            vectors = asyncio.run(embed_documents(texts))
        # Charged to no one: documents' chunks as ingest, everything else as indexing.
        for purpose, kinds in _PURPOSES:
            charged = [t for r, t in zip(rows, texts, strict=True) if kinds(r.kind)]
            usage.record_embedding(Charge(data_store_id, purpose), charged)
        _store_embeddings(data_store_id, rows, vectors)
        done += len(rows) + _reuse(data_store_id)
        if time.monotonic() >= deadline:
            break
    return done
