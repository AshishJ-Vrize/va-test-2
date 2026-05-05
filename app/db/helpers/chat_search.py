"""Hybrid chunk search with round-robin diversification across meetings.

Public entry point: `hybrid_chunk_search()`.

Two retrieval signals:
  - Vector cosine over `chunks.embedding` (1536-dim, pgvector)
  - BM25 over `chunks.search_vector` (Postgres tsvector, GENERATED from search_text)

Each signal contributes a ranked list of `pool = top_k * 2` candidates. The
two lists are merged via Reciprocal Rank Fusion (RRF, K=60), then diversified
with a round-robin pass that ensures every meeting in scope gets a turn
before any single meeting hogs the top-k.

Filters
-------
    meeting_ids        : RBAC-bounded set of meetings to search inside (required)
    speaker_graph_ids  : optional — restrict to chunks where at least one of
                         these graph IDs appears in `speakers_graph_ids`

Date filters are NOT applied here — the caller (orchestrator) narrows the
meeting_ids set first based on date, so by the time the searcher runs all
in-scope meetings are date-valid.

Three internals are split out as pure functions so tests can validate the
diversification + fusion logic without touching a database:

    _rrf_merge()              — merge two ranked lists by RRF score
    _round_robin_by_meeting() — interleave by meeting_id, preserve internal order
    _row_to_chunk()           — DB row → RetrievedChunk dataclass
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.config import (
    RETRIEVAL_POOL_MULTIPLIER,
    RETRIEVAL_RRF_K,
    SEARCH_TOP_K,
)
from app.services.chat.interfaces import RetrievedChunk

log = logging.getLogger(__name__)

# Re-export the config values under the historical names so existing tests
# that imported `RRF_K` etc. from this module keep working.
RRF_K = RETRIEVAL_RRF_K
DEFAULT_TOP_K = SEARCH_TOP_K
POOL_MULTIPLIER = RETRIEVAL_POOL_MULTIPLIER

# ── BM25 query construction ───────────────────────────────────────────────────

_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "what", "which",
    "who", "this", "that", "and", "but", "or", "not", "how", "why",
    "when", "where", "my", "your", "his", "her", "their", "our", "its",
    "about", "said", "say", "tell", "mentioned", "meeting", "discuss",
}


def _build_or_tsquery(query_text: str) -> str | None:
    """Lowercased, punctuation-stripped tsquery using OR of significant words.

    Uses 'simple' tsquery config (no stemming) to preserve proper nouns like
    names and acronyms. Returns None if no significant terms remain after
    stop-word filtering — caller skips BM25 in that case.

    Strips English possessive `'s` first so "Acme's" → "acme" (not "acmes").
    Without this, BM25 searches against `'simple'`-tokenised indexes miss the
    base form because the query carries the trailing 's'.
    """
    cleaned = re.sub(r"'s\b", "", query_text.lower())
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    words = cleaned.split()
    significant = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    if not significant:
        return None
    return " | ".join(significant)


# ── Public API ────────────────────────────────────────────────────────────────

async def hybrid_chunk_search(
    db: AsyncSession,
    query_embedding: list[float],
    query_text: str,
    meeting_ids: list[uuid.UUID],
    speaker_graph_ids: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[RetrievedChunk]:
    """Hybrid (BM25 + cosine) chunk search with RRF + round-robin diversification.

    Returns at most `top_k` chunks. Empty list if no results or no meeting_ids.
    Each returned chunk is dressed in the dataclass shape consumed by handlers.
    """
    if not meeting_ids:
        return []

    pool = top_k * POOL_MULTIPLIER
    ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"
    gids_str = (
        "{" + ",".join(speaker_graph_ids) + "}"
        if speaker_graph_ids else None
    )

    vector_candidates, bm25_candidates = await asyncio.gather(
        _fetch_vector_candidates(db, query_embedding, ids_str, gids_str, pool),
        _fetch_bm25_candidates(db, query_text, ids_str, gids_str, pool),
    )

    fused = _rrf_merge(vector_candidates, bm25_candidates, rrf_k=RRF_K)
    return _round_robin_by_meeting(fused, top_k=top_k)


# ── DB-touching internals ─────────────────────────────────────────────────────

async def _fetch_vector_candidates(
    db: AsyncSession,
    query_embedding: list[float],
    ids_str: str,
    gids_str: str | None,
    pool: int,
) -> list[RetrievedChunk]:
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    sql = text("""
        SELECT
            c.id              AS chunk_id,
            c.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date,
            c.speakers,
            c.chunk_text,
            c.start_ms,
            c.end_ms,
            1.0 - (c.embedding <=> CAST(:embedding AS vector)) AS score
        FROM chunks c
        JOIN meetings m ON c.meeting_id = m.id
        WHERE c.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.embedding IS NOT NULL
          AND (CAST(:gids AS text[]) IS NULL
               OR CAST(:gids AS text[]) && c.speaker_graph_ids)
        ORDER BY c.embedding <=> CAST(:embedding AS vector)
        LIMIT :pool
    """)
    rows = await db.execute(sql, {
        "embedding": embedding_str,
        "ids": ids_str,
        "gids": gids_str,
        "pool": pool,
    })
    return [_row_to_chunk(r) for r in rows]


async def _fetch_bm25_candidates(
    db: AsyncSession,
    query_text: str,
    ids_str: str,
    gids_str: str | None,
    pool: int,
) -> list[RetrievedChunk]:
    or_query = _build_or_tsquery(query_text)
    if not or_query:
        return []

    sql = text("""
        SELECT
            c.id              AS chunk_id,
            c.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date,
            c.speakers,
            c.chunk_text,
            c.start_ms,
            c.end_ms,
            ts_rank_cd(c.search_vector, to_tsquery('simple', :or_query)) AS score
        FROM chunks c
        JOIN meetings m ON c.meeting_id = m.id
        WHERE c.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.search_vector @@ to_tsquery('simple', :or_query)
          AND (CAST(:gids AS text[]) IS NULL
               OR CAST(:gids AS text[]) && c.speaker_graph_ids)
        ORDER BY score DESC
        LIMIT :pool
    """)
    try:
        rows = await db.execute(sql, {
            "or_query": or_query,
            "ids": ids_str,
            "gids": gids_str,
            "pool": pool,
        })
        return [_row_to_chunk(r) for r in rows]
    except Exception:
        # BM25 fails open — degrade to vector-only rather than fail the request.
        log.warning("BM25 query failed; degrading to vector-only", exc_info=True)
        return []


def _row_to_chunk(r: Any) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=r.chunk_id,
        meeting_id=r.meeting_id,
        meeting_title=r.meeting_title or "",
        meeting_date=r.meeting_date,
        speakers=list(r.speakers or []),
        chunk_text=list(r.chunk_text or []),
        start_ms=r.start_ms or 0,
        end_ms=r.end_ms or 0,
        score=float(r.score),
    )


# ── Pure functions (DB-free, fully unit-testable) ─────────────────────────────

def _rrf_merge(
    vector: list[RetrievedChunk],
    bm25: list[RetrievedChunk],
    rrf_k: int = RRF_K,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion: score(d) = Σ 1 / (rrf_k + rank+1).

    Deduplicates by chunk_id. Returns chunks sorted by fused score (descending),
    each chunk's `score` field replaced with the fused score for downstream use.
    """
    scores: dict[uuid.UUID, float] = {}
    chunks: dict[uuid.UUID, RetrievedChunk] = {}

    for rank, item in enumerate(vector):
        cid = item.chunk_id
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        chunks[cid] = item

    for rank, item in enumerate(bm25):
        cid = item.chunk_id
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        if cid not in chunks:
            chunks[cid] = item

    ranked_ids = sorted(scores, key=lambda c: scores[c], reverse=True)
    fused: list[RetrievedChunk] = []
    for cid in ranked_ids:
        item = chunks[cid]
        # Re-stamp the score field with the fused value (handlers/UI use it).
        fused.append(RetrievedChunk(
            chunk_id=item.chunk_id,
            meeting_id=item.meeting_id,
            meeting_title=item.meeting_title,
            meeting_date=item.meeting_date,
            speakers=list(item.speakers),
            chunk_text=list(item.chunk_text),
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            score=round(scores[cid], 6),
        ))
    return fused


def _round_robin_by_meeting(
    candidates: list[RetrievedChunk],
    top_k: int = DEFAULT_TOP_K,
) -> list[RetrievedChunk]:
    """Diversify across meetings.

    Groups candidates by meeting_id (preserving each group's internal order
    by RRF score), then walks meetings in first-appearance order, pulling
    one chunk per meeting per cycle until top_k is reached or all groups
    are empty. High-RRF chunks still rise — but every represented meeting
    contributes at least one chunk before any one meeting contributes two.

    Single-meeting input → degenerates to first top_k by RRF (no-op).
    """
    if not candidates:
        return []
    if top_k <= 0:
        return []

    by_meeting: dict[uuid.UUID, list[RetrievedChunk]] = {}
    order: list[uuid.UUID] = []   # first-appearance order
    for c in candidates:
        if c.meeting_id not in by_meeting:
            by_meeting[c.meeting_id] = []
            order.append(c.meeting_id)
        by_meeting[c.meeting_id].append(c)

    result: list[RetrievedChunk] = []
    while len(result) < top_k:
        progressed = False
        for mid in order:
            if not by_meeting[mid]:
                continue
            result.append(by_meeting[mid].pop(0))
            progressed = True
            if len(result) >= top_k:
                break
        if not progressed:
            break
    return result
