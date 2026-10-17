"""Hybrid retrieval: pgvector cosine search + Postgres full-text search, fused with RRF, over a
data store's documents, or over its exported run history (in its own database, as its read-only
role)."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotReady,
    store_session,
    store_status,
)
from aios.datastores.history import NoHistory, check_searchable
from aios.datastores.models import EMBEDDABLE
from aios.embeddings import embed_query
from aios.models import DataStoreStatus

log = logging.getLogger(__name__)

# Each branch ranks its own top-N candidates; Reciprocal Rank Fusion then scores every
# chunk as sum(1 / (k + rank)) over the branches it appears in.
HYBRID_SQL = text(
    """
    WITH semantic AS (
        SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rank
        FROM aios.chunks c
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :candidates
    ),
    lexical AS (
        SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rank
        FROM aios.chunks c,
             websearch_to_tsquery('english', :query) q
        WHERE c.tsv @@ q
        ORDER BY ts_rank_cd(c.tsv, q) DESC
        LIMIT :candidates
    ),
    fused AS (
        SELECT COALESCE(s.id, l.id) AS id,
               COALESCE(1.0 / (:rrf_k + s.rank), 0) + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS score,
               s.rank AS semantic_rank,
               l.rank AS lexical_rank
        FROM semantic s
        FULL OUTER JOIN lexical l ON l.id = s.id
    )
    SELECT f.id, f.score, f.semantic_rank, f.lexical_rank,
           c.text, c.meta, c.document_id, d.filename
    FROM fused f
    JOIN aios.chunks c ON c.id = f.id
    JOIN aios.documents d ON d.id = c.document_id
    ORDER BY f.score DESC
    LIMIT :limit
    """
)


# The same over the store's exported messages (`aios.datastores.history`), keyed by run and
# position: the semantic branch over those embedded (while embedding is on), the lexical one over
# every one worth searching. The run searching, if any, is left out: it's not a past run. With
# no query vector (the embedding API failed), it's the lexical branch alone.
HISTORY_SQL = text(
    f"""
    WITH semantic AS (
        SELECT m.agent_run_id, m.position,
               row_number() OVER (ORDER BY m.embedding <=> CAST(:qvec AS vector)) AS rank
        FROM history.messages m
        WHERE CAST(:qvec AS vector) IS NOT NULL
          AND m.embedding IS NOT NULL AND m.agent_run_id <> :exclude
        ORDER BY m.embedding <=> CAST(:qvec AS vector)
        LIMIT :candidates
    ),
    lexical AS (
        SELECT m.agent_run_id, m.position,
               row_number() OVER (ORDER BY ts_rank_cd(m.tsv, q) DESC) AS rank
        FROM history.messages m,
             websearch_to_tsquery('english', :query) q
        WHERE m.tsv @@ q AND {EMBEDDABLE} AND m.agent_run_id <> :exclude
        ORDER BY ts_rank_cd(m.tsv, q) DESC
        LIMIT :candidates
    ),
    fused AS (
        SELECT COALESCE(s.agent_run_id, l.agent_run_id) AS agent_run_id,
               COALESCE(s.position, l.position) AS position,
               COALESCE(1.0 / (:rrf_k + s.rank), 0) + COALESCE(1.0 / (:rrf_k + l.rank), 0) AS score,
               s.rank AS semantic_rank,
               l.rank AS lexical_rank
        FROM semantic s
        FULL OUTER JOIN lexical l
            ON l.agent_run_id = s.agent_run_id AND l.position = s.position
    )
    SELECT f.agent_run_id, f.position, f.score, f.semantic_rank, f.lexical_rank,
           m.role, m.text, r.agent_name, r.title, r.created_at
    FROM fused f
    JOIN history.messages m ON m.agent_run_id = f.agent_run_id AND m.position = f.position
    JOIN history.agent_runs r ON r.id = f.agent_run_id
    ORDER BY f.score DESC
    LIMIT :limit
    """
)


# What the search tools tell the model while the store is being set up.
STORE_NOT_READY = "The user's data store is still being set up, so there's nothing to search yet."
# Or when setting it up failed (as the API words it: a retry follows, later).
STORE_FAILED = (
    "The user's data store couldn't be set up, so there's nothing to search. "
    "It will be tried again; they can check back later."
)
# Or while it's brought to the store tree's head (made by the release before, during a deploy).
STORE_UPGRADING = "The user's data store is being upgraded. Try the search again in a moment."
# And while it has no connections free.
STORE_BUSY = "The user's data store is busy. Try the search again in a moment."
# What the history search tells it when the store has no exported history.
NO_HISTORY = (
    "The user hasn't exported their run history, so there are no past runs to search. "
    "They can switch the export on in Settings."
)


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    filename: str
    text: str
    meta: dict
    score: float
    semantic_rank: int | None
    lexical_rank: int | None


@dataclass
class HistoryHit:
    agent_run_id: int
    position: int
    agent_name: str
    title: str | None
    created_at: datetime
    role: str
    text: str
    score: float
    semantic_rank: int | None
    lexical_rank: int | None


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def hybrid_search_with_vector(
    db: Session, *, query: str, query_vector: list[float], limit: int = 8
) -> list[SearchHit]:
    """Search the documents of the store `db` is a session on."""
    settings = get_settings()
    # pgvector >= 0.8: keep scanning the HNSW index past `hnsw.ef_search` (40) candidates when
    # more are asked for.
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = db.execute(
        HYBRID_SQL,
        {
            "qvec": vector_literal(query_vector),
            "query": query,
            # Enough candidates from each branch to fill `limit` even when only one matches.
            "candidates": max(settings.search_candidates, limit),
            "rrf_k": settings.search_rrf_k,
            "limit": limit,
        },
    ).mappings()
    hits = [
        SearchHit(
            chunk_id=r["id"],
            document_id=r["document_id"],
            filename=r["filename"],
            text=r["text"],
            meta=r["meta"] or {},
            score=float(r["score"]),
            semantic_rank=r["semantic_rank"],
            lexical_rank=r["lexical_rank"],
        )
        for r in rows
    ]
    db.rollback()  # end the transaction so SET LOCAL doesn't leak
    return hits


def history_search_with_vector(
    db: Session,
    *,
    query: str,
    query_vector: list[float] | None,
    limit: int = 8,
    exclude_run_id: int | None = None,
) -> list[HistoryHit]:
    """Search the exported messages of the store `db` is a session on, but `exclude_run_id`'s;
    by keyword alone with no `query_vector`."""
    settings = get_settings()
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = db.execute(
        HISTORY_SQL,
        {
            "qvec": vector_literal(query_vector) if query_vector else None,
            "query": query,
            "candidates": max(settings.search_candidates, limit),
            "rrf_k": settings.search_rrf_k,
            "limit": limit,
            # Platform ids start at 1.
            "exclude": exclude_run_id or 0,
        },
    ).mappings()
    hits = [
        HistoryHit(
            agent_run_id=r["agent_run_id"],
            position=r["position"],
            agent_name=r["agent_name"],
            title=r["title"],
            created_at=r["created_at"],
            role=r["role"],
            text=r["text"],
            score=float(r["score"]),
            semantic_rank=r["semantic_rank"],
            lexical_rank=r["lexical_rank"],
        )
        for r in rows
    ]
    db.rollback()  # end the transaction so SET LOCAL doesn't leak
    return hits


def _check_ready(data_store_id: uuid.UUID) -> None:
    status = store_status(data_store_id)
    if status != DataStoreStatus.ready:
        raise DataStoreNotReady(data_store_id, status)


async def _search[T](
    data_store_id: uuid.UUID,
    query: str,
    search: Callable[[Session, list[float] | None], list[T]],
    *,
    history: bool = False,
) -> list[T]:
    """Embed `query` and run `search` with it, on a read-only session on the store. For the
    store's `history`, it searches without the vector (by keyword) if embedding fails: the
    history may have no embeddings anyway (embedding is opt-in), and keyword search needs none."""
    if not query.strip():  # e.g. a blank query from the agent's tool call
        return []
    # Embedding the query would be wasted on a store that isn't ready. The session is opened
    # only after it: an engine held across the embed may have been evicted by then.
    await to_thread.run_sync(check_searchable if history else _check_ready, data_store_id)
    try:
        query_vector = await embed_query(query)
    except Exception:
        if not history:
            raise
        log.warning(
            "Couldn't embed the history search's query; searching by keyword", exc_info=True
        )
        query_vector = None

    def run() -> list[T]:
        with store_session(data_store_id, "ro") as db:
            return search(db, query_vector)

    return await to_thread.run_sync(run)


async def hybrid_search(*, data_store_id: uuid.UUID, query: str, limit: int = 8) -> list[SearchHit]:
    """Search the data store's documents."""
    return await _search(
        data_store_id,
        query,
        lambda db, vec: hybrid_search_with_vector(db, query=query, query_vector=vec, limit=limit),
    )


async def history_search(
    *,
    data_store_id: uuid.UUID,
    query: str,
    limit: int = 8,
    exclude_run_id: int | None = None,
) -> list[HistoryHit]:
    """Search the data store's exported run history (but `exclude_run_id`'s messages). Raises
    `NoHistory` if it has none."""
    return await _search(
        data_store_id,
        query,
        lambda db, vec: history_search_with_vector(
            db, query=query, query_vector=vec, limit=limit, exclude_run_id=exclude_run_id
        ),
        history=True,
    )


async def _tool_results(search: Callable[[], Awaitable[list[dict]]]) -> list[dict]:
    """The search's results for the model, or why there are none."""
    try:
        return await search()
    except NoHistory:
        return [{"error": NO_HISTORY}]
    except DataStoreBehind:
        return [{"error": STORE_UPGRADING}]
    except DataStoreNotReady as exc:
        failed = exc.status == DataStoreStatus.failed
        return [{"error": STORE_FAILED if failed else STORE_NOT_READY}]
    except DataStoreBusy:
        return [{"error": STORE_BUSY}]


async def document_search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8
) -> list[dict]:
    """Hybrid search hits in the form the search tools return to the model."""

    async def search() -> list[dict]:
        hits = await hybrid_search(data_store_id=data_store_id, query=query, limit=limit)
        return [
            {
                "source": hit.filename,
                "headings": hit.meta.get("headings", []),
                "text": hit.text,
            }
            for hit in hits
        ]

    return await _tool_results(search)


async def history_search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8, exclude_run_id: int | None = None
) -> list[dict]:
    """History search hits in the form the history search tool returns to the model: each
    message with the run it's from, and when that run started."""

    async def search() -> list[dict]:
        hits = await history_search(
            data_store_id=data_store_id, query=query, limit=limit, exclude_run_id=exclude_run_id
        )
        return [
            {
                "run_id": hit.agent_run_id,
                "agent": hit.agent_name,
                "title": hit.title,
                "when": hit.created_at.isoformat(),
                "role": hit.role,
                "text": hit.text,
            }
            for hit in hits
        ]

    return await _tool_results(search)
