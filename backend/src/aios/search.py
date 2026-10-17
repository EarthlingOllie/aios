"""Hybrid retrieval: pgvector cosine search + Postgres full-text search, fused with RRF, over a
data store's documents (in its own database, as its read-only role)."""

import uuid
from dataclasses import dataclass

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores import (
    DataStoreBusy,
    DataStoreNotReady,
    store_session,
    store_status,
)
from aios.datastores.notices import store_unusable
from aios.embeddings import embed_query
from aios.models import DataStoreStatus

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


async def hybrid_search(*, data_store_id: uuid.UUID, query: str, limit: int = 8) -> list[SearchHit]:
    """Search the data store's documents."""
    if not query.strip():  # e.g. a blank query from the agent's tool call
        return []
    # Embedding the query would be wasted on a store that isn't ready. The session is opened
    # only after it: an engine held across the embed may have been evicted by then.
    status = await to_thread.run_sync(store_status, data_store_id)
    if status != DataStoreStatus.ready:
        raise DataStoreNotReady(data_store_id, status)
    query_vector = await embed_query(query)

    def run() -> list[SearchHit]:
        with store_session(data_store_id, "ro") as db:
            return hybrid_search_with_vector(
                db, query=query, query_vector=query_vector, limit=limit
            )

    return await to_thread.run_sync(run)


async def document_search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8
) -> list[dict]:
    """Hybrid search hits in the form the search tools return to the model."""
    try:
        hits = await hybrid_search(data_store_id=data_store_id, query=query, limit=limit)
    except (DataStoreNotReady, DataStoreBusy) as exc:
        return [{"error": store_unusable(exc)}]
    return [
        {
            "source": hit.filename,
            "headings": hit.meta.get("headings", []),
            "text": hit.text,
        }
        for hit in hits
    ]
