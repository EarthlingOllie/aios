"""Unified search: hybrid retrieval (pgvector cosine search + Postgres full-text search, fused
with RRF) over a data store's search index (`aios.search_index`): its documents' chunks, its
chat messages, its runs' messages and its briefs, in its own database, as its read-only role.

Everyone in the organisation can find everything, so nothing here filters by who's asking. But
it finds only items whose Jev verdict is clean, released or derived (`aios.verdicts`): flagged
items, and items still awaiting Jev's check, are left out. An
item not yet embedded is found by keyword alone, and so is everything when the query can't be
embedded (the embedding API is down): an outage doesn't blind search.

The search tool (`aios.tools.search`) and the Search page's endpoint search every kind; the
document search and history search tools are the same search fixed to their kind, with their
own result shapes (`document_search_results`, `history_search_results`).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.datastores import (
    DataStoreBehind,
    DataStoreBusy,
    DataStoreNotReady,
    store_busy,
    store_not_ready,
    store_session,
    store_status,
    store_upgrading,
)
from aios.datastores.models import SearchItemKind
from aios.embeddings import embed_query
from aios.jev import VISIBLE
from aios.models import DataStoreStatus

log = logging.getLogger(__name__)


# Each branch ranks its own top-N candidates; Reciprocal Rank Fusion then scores every item as
# sum(1 / (k + rank)) over the branches it appears in. With no query vector, it's the lexical
# branch alone. `{where}` is the filters, the same in both branches.
SEARCH_SQL = """
    WITH semantic AS (
        SELECT i.id, row_number() OVER (ORDER BY i.embedding <=> CAST(:qvec AS vector)) AS rank
        FROM aios.search_items i
        WHERE CAST(:qvec AS vector) IS NOT NULL AND i.embedding IS NOT NULL{where}
        ORDER BY i.embedding <=> CAST(:qvec AS vector)
        LIMIT :candidates
    ),
    lexical AS (
        SELECT i.id, row_number() OVER (ORDER BY ts_rank_cd(i.tsv, q) DESC) AS rank
        FROM aios.search_items i,
             websearch_to_tsquery('english', :query) q
        WHERE i.tsv @@ q{where}
        ORDER BY ts_rank_cd(i.tsv, q) DESC
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
           i.kind, i.text, i.ref, i.created_at, i.document_id, i.chat_id, i.agent_id,
           i.agent_run_id, i.message_id, i.invocation_id, i.author_user_id,
           d.filename, c.name AS chat_name, a.name AS agent_name,
           r.title AS run_title, r.created_at AS run_created_at
    FROM fused f
    JOIN aios.search_items i ON i.id = f.id
    LEFT JOIN aios.documents d ON d.id = i.document_id
    LEFT JOIN aios.chats c ON c.id = i.chat_id
    LEFT JOIN aios.agents a ON a.id = i.agent_id
    LEFT JOIN aios.agent_runs r ON r.id = i.agent_run_id
    ORDER BY f.score DESC, f.id
    LIMIT :limit
"""


# What the search tools tell the model while the store is brought to the store tree's head,
# or has no connections free (and, from `store_not_ready`, while it isn't ready).
STORE_UPGRADING = store_upgrading("the search")
STORE_BUSY = store_busy("the search")


@dataclass(frozen=True)
class Filters:
    """What a search is narrowed to: any of these kinds (all, if none), written in this time
    range, in this chat, by or for this agent; and never this run's messages (the run
    searching: it's not a past run)."""

    kinds: Sequence[SearchItemKind] = ()
    since: datetime | None = None
    until: datetime | None = None
    chat_id: int | None = None
    agent_id: int | None = None
    exclude_run_id: int | None = None

    def sql(self) -> tuple[str, dict[str, Any]]:
        # Only what may reach a model: never flagged items, nor any Jev hasn't checked yet.
        where = ["i.verdict::text = ANY(:visible)"]
        params: dict[str, Any] = {"visible": [v.value for v in VISIBLE]}
        if self.kinds:
            where.append("i.kind::text = ANY(:kinds)")
            params["kinds"] = [SearchItemKind(k).value for k in self.kinds]
        if self.since is not None:
            where.append("i.created_at >= :since")
            params["since"] = self.since
        if self.until is not None:
            where.append("i.created_at < :until")
            params["until"] = self.until
        if self.chat_id is not None:
            where.append("i.chat_id = :chat_id")
            params["chat_id"] = self.chat_id
        if self.agent_id is not None:
            where.append("i.agent_id = :agent_id")
            params["agent_id"] = self.agent_id
        if self.exclude_run_id is not None:
            where.append("i.agent_run_id IS DISTINCT FROM :exclude_run_id")
            params["exclude_run_id"] = self.exclude_run_id
        return "".join(f" AND {w}" for w in where), params


# No filters: every item.
EVERYTHING = Filters()


@dataclass
class SearchHit:
    """An item found: what it is, its text, where it's from (for its link), and how each branch
    ranked it."""

    id: int
    kind: SearchItemKind
    text: str
    created_at: datetime
    score: float
    semantic_rank: int | None
    lexical_rank: int | None
    ref: dict
    # Its source, by kind: a chunk's document; a chat message's chat (and message); a run
    # message's run, its agent and (if a chat started it) chat; a brief's chat and agent.
    document_id: int | None = None
    filename: str | None = None
    chat_id: int | None = None
    chat_name: str | None = None
    message_id: int | None = None
    agent_id: int | None = None
    agent_name: str | None = None
    agent_run_id: int | None = None
    run_title: str | None = None
    run_created_at: datetime | None = None
    invocation_id: int | None = None
    author_user_id: int | None = None


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def _iterative_scan(db: Session) -> bool:
    """Whether the store's pgvector has iterative index scans (0.8 and later)."""
    version = db.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    try:
        return tuple(int(p) for p in (version or "0").split(".")[:2]) >= (0, 8)
    except ValueError:
        return False


def search_with_vector(
    db: Session,
    *,
    query: str,
    query_vector: list[float] | None,
    limit: int = 8,
    filters: Filters = EVERYTHING,
) -> list[SearchHit]:
    """Search the index of the store `db` is a session on; by keyword alone with no
    `query_vector`."""
    settings = get_settings()
    where, params = filters.sql()
    # pgvector >= 0.8: keep scanning the HNSW index past `hnsw.ef_search` (40) candidates when
    # more are asked for, or filters leave out the nearest. An older one has no such setting,
    # and refuses it once loaded.
    if _iterative_scan(db):
        db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = (
        db.execute(
            text(SEARCH_SQL.format(where=where)),
            {
                "qvec": vector_literal(query_vector) if query_vector else None,
                "query": query,
                # Enough candidates from each branch to fill `limit` even when only one matches.
                "candidates": max(settings.search_candidates, limit),
                "rrf_k": settings.search_rrf_k,
                "limit": limit,
                **params,
            },
        )
        .mappings()
        .all()
    )
    db.rollback()  # end the transaction so SET LOCAL doesn't leak
    return [
        SearchHit(
            id=r["id"],
            kind=SearchItemKind(r["kind"]),
            text=r["text"],
            created_at=r["created_at"],
            score=float(r["score"]),
            semantic_rank=r["semantic_rank"],
            lexical_rank=r["lexical_rank"],
            ref=r["ref"] or {},
            document_id=r["document_id"],
            filename=r["filename"],
            chat_id=r["chat_id"],
            chat_name=r["chat_name"],
            message_id=r["message_id"],
            agent_id=r["agent_id"],
            agent_name=r["agent_name"],
            agent_run_id=r["agent_run_id"],
            run_title=r["run_title"],
            run_created_at=r["run_created_at"],
            invocation_id=r["invocation_id"],
            author_user_id=r["author_user_id"],
        )
        for r in rows
    ]


def _check_ready(data_store_id: uuid.UUID) -> None:
    status = store_status(data_store_id)
    if status != DataStoreStatus.ready:
        raise DataStoreNotReady(data_store_id, status)


async def search(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8, filters: Filters = EVERYTHING
) -> list[SearchHit]:
    """Embed `query` and search the store's index with it, on a read-only session on the store;
    by keyword alone if the query can't be embedded."""
    if not query.strip():  # e.g. a blank query from the agent's tool call
        return []
    # Embedding the query would be wasted on a store that isn't ready. The session is opened
    # only after it: an engine held across the embed may have been evicted by then.
    await to_thread.run_sync(_check_ready, data_store_id)
    try:
        query_vector = await embed_query(query)
    except Exception:
        log.warning("Couldn't embed the search's query; searching by keyword", exc_info=True)
        query_vector = None

    def run() -> list[SearchHit]:
        with store_session(data_store_id, "ro") as db:
            return search_with_vector(
                db, query=query, query_vector=query_vector, limit=limit, filters=filters
            )

    return await to_thread.run_sync(run)


async def tool_results(found: Callable[[], Awaitable[list[dict]]]) -> list[dict]:
    """The search's results for the model, or why there are none."""
    try:
        return await found()
    except DataStoreBehind:
        return [{"error": STORE_UPGRADING}]
    except DataStoreNotReady as exc:
        return [{"error": store_not_ready(exc.status)}]
    except DataStoreBusy:
        return [{"error": STORE_BUSY}]


def item_result(hit: SearchHit) -> dict:
    """An item as the search tool returns it to the model: its kind, text and when it was
    written, with where it's from."""
    result: dict[str, Any] = {"kind": hit.kind.value, "text": hit.text}
    if hit.kind == SearchItemKind.document_chunk:
        result |= {"source": hit.filename, "headings": hit.ref.get("headings", [])}
    elif hit.kind == SearchItemKind.run_message:
        result |= {
            "run_id": hit.agent_run_id,
            "agent": hit.agent_name,
            "title": hit.run_title,
            "role": hit.ref.get("role"),
        }
    else:
        result |= {"agent": hit.agent_name} if hit.agent_name else {}
    if hit.chat_id is not None:
        result |= {"chat_id": hit.chat_id, "chat": hit.chat_name}
    result["when"] = hit.created_at.isoformat()
    return result


async def search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8, filters: Filters = EVERYTHING
) -> list[dict]:
    """Search hits in the form the search tool returns to the model."""

    async def found() -> list[dict]:
        hits = await search(data_store_id=data_store_id, query=query, limit=limit, filters=filters)
        return [item_result(hit) for hit in hits]

    return await tool_results(found)


async def document_search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8
) -> list[dict]:
    """Document chunks found, in the form the document search tool returns to the model."""

    async def found() -> list[dict]:
        hits = await search(
            data_store_id=data_store_id,
            query=query,
            limit=limit,
            filters=Filters(kinds=[SearchItemKind.document_chunk]),
        )
        return [
            {"source": hit.filename, "headings": hit.ref.get("headings", []), "text": hit.text}
            for hit in hits
        ]

    return await tool_results(found)


async def history_search_results(
    *, data_store_id: uuid.UUID, query: str, limit: int = 8, exclude_run_id: int | None = None
) -> list[dict]:
    """Past runs' messages found (but `exclude_run_id`'s), in the form the history search tool
    returns to the model: each message with the run it's from, and when that run started."""

    async def found() -> list[dict]:
        hits = await search(
            data_store_id=data_store_id,
            query=query,
            limit=limit,
            filters=Filters(kinds=[SearchItemKind.run_message], exclude_run_id=exclude_run_id),
        )
        return [
            {
                "run_id": hit.agent_run_id,
                "agent": hit.agent_name,
                "title": hit.run_title,
                "when": hit.run_created_at.isoformat() if hit.run_created_at else None,
                "role": hit.ref.get("role"),
                "text": hit.text,
            }
            for hit in hits
        ]

    return await tool_results(found)
