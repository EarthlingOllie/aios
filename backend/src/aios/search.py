"""Hybrid retrieval: pgvector cosine search + Postgres full-text search, fused with RRF."""

from dataclasses import dataclass

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.db import SessionLocal
from aios.embeddings import embed_query

# Each branch ranks its own top-N candidates; Reciprocal Rank Fusion then scores every
# chunk as sum(1 / (k + rank)) over the branches it appears in.
HYBRID_SQL = text(
    """
    WITH semantic AS (
        SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS rank
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.owner_id = :owner_id
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :candidates
    ),
    lexical AS (
        SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rank
        FROM chunks c
        JOIN documents d ON d.id = c.document_id,
             websearch_to_tsquery('english', :query) q
        WHERE d.owner_id = :owner_id AND c.tsv @@ q
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
    JOIN chunks c ON c.id = f.id
    JOIN documents d ON d.id = c.document_id
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
    db: Session, *, owner_id: int, query: str, query_vector: list[float], limit: int = 8
) -> list[SearchHit]:
    settings = get_settings()
    # pgvector >= 0.8: keep scanning the HNSW index until enough rows pass the owner filter.
    db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = db.execute(
        HYBRID_SQL,
        {
            "qvec": vector_literal(query_vector),
            "query": query,
            "owner_id": owner_id,
            "candidates": settings.search_candidates,
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


async def hybrid_search(*, owner_id: int, query: str, limit: int = 8) -> list[SearchHit]:
    if not query.strip():  # e.g. a blank query from the agent's tool call
        return []
    query_vector = await embed_query(query)

    def run() -> list[SearchHit]:
        with SessionLocal() as db:
            return hybrid_search_with_vector(
                db, owner_id=owner_id, query=query, query_vector=query_vector, limit=limit
            )

    return await to_thread.run_sync(run)


async def document_search_results(*, owner_id: int, query: str, limit: int = 8) -> list[dict]:
    """Hybrid search hits in the form the search tools return to the model."""
    hits = await hybrid_search(owner_id=owner_id, query=query, limit=limit)
    return [
        {
            "source": hit.filename,
            "headings": hit.meta.get("headings", []),
            "text": hit.text,
        }
        for hit in hits
    ]
