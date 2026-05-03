"""SEARCH route handler — hybrid BM25 + pgvector with Reciprocal Rank Fusion.

Reads the v2 chunks schema:
    chunk_text JSONB, speakers TEXT[], speaker_graph_ids TEXT[],
    search_vector tsvector (generated), embedding vector(1536)

Hard filters at SQL level:
- meeting_id ∈ authorized set (RBAC, enforced by caller)
- date range (date_from / date_to)
- speaker_graph_ids overlap (only when router resolved the name)

Soft filters via ranking:
- speaker name (folded into BM25 search_text — speakers are tokens in it)
- keyword (folded into BM25 — the user's significant terms always reach BM25)

ILIKE substring filtering on text/speaker is gone — replaced by indexed
array overlap (`&&`) for speakers and tsvector matching for content.
"""
from __future__ import annotations

import asyncio
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SIMILARITY_THRESHOLD = 0.60
_TOP_K = 10
_RRF_K = 60  # standard RRF constant — higher = smoother rank blending

# Common English stop words to exclude when building OR tsquery.
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "what", "which",
    "who", "this", "that", "and", "but", "or", "not", "how", "why",
    "when", "where", "my", "your", "his", "her", "their", "our", "its",
    "about", "said", "say", "tell", "mentioned", "meeting", "discuss",
}


def _build_or_tsquery(text_: str) -> str | None:
    """Convert a natural-language query into a to_tsquery OR expression.

    Uses 'simple' config (no stemming) to preserve proper nouns like names.
    Returns None if no significant terms remain after stop-word filtering.
    """
    words = re.sub(r"[^\w\s]", "", text_.lower()).split()
    significant = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    if not significant:
        return None
    return " | ".join(significant)


async def handle_search(
    query_embedding: list[float],
    query_text: str,
    authorized_meeting_ids: list[uuid.UUID],
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    """Hybrid retrieval: pgvector cosine + Postgres BM25, fused via RRF.

    Each method contributes up to _TOP_K candidates; RRF re-ranks the union
    and returns the best _TOP_K overall. BM25 catches exact-match terms
    (names, acronyms); vector catches semantic matches (paraphrases).
    """
    if not authorized_meeting_ids:
        return []

    ids_str = "{" + ",".join(str(mid) for mid in authorized_meeting_ids) + "}"

    # speaker_graph_ids filter — applied only when router resolved a name to
    # one or more graph IDs. Empty list = "we tried and found nothing" — we
    # treat that as no filter so ranking still surfaces relevant chunks.
    gids = filters.get("speaker_graph_ids") or []
    gids_str = "{" + ",".join(gids) + "}" if gids else None

    vector_results, bm25_results = await asyncio.gather(
        _vector_search(query_embedding, ids_str, gids_str, filters, db),
        _bm25_search(query_text, ids_str, gids_str, filters, db),
    )
    return _rrf_merge(vector_results, bm25_results, top_k=_TOP_K)


async def _vector_search(
    query_embedding: list[float],
    ids_str: str,
    gids_str: str | None,
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

    sql = text("""
        SELECT
            c.id              AS chunk_id,
            c.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date,
            c.speakers,
            c.chunk_text,
            c.start_ms,
            c.end_ms,
            1.0 - (c.embedding <=> CAST(:embedding AS vector)) AS similarity_score
        FROM chunks c
        JOIN meetings m ON c.meeting_id = m.id
        WHERE c.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.embedding IS NOT NULL
          AND 1.0 - (c.embedding <=> CAST(:embedding AS vector)) > :threshold
          AND (CAST(:gids AS text[]) IS NULL OR CAST(:gids AS text[]) && c.speaker_graph_ids)
          AND (CAST(:date_from AS timestamptz) IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (CAST(:date_to   AS timestamptz) IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
        ORDER BY similarity_score DESC
        LIMIT :top_k
    """)
    rows = await db.execute(sql, {
        "embedding": embedding_str,
        "ids": ids_str,
        "threshold": _SIMILARITY_THRESHOLD,
        "gids": gids_str,
        "date_from": date_from,
        "date_to": date_to,
        "top_k": _TOP_K,
    })
    return [_row_to_dict(r, float(r.similarity_score)) for r in rows]


async def _bm25_search(
    query_text: str,
    ids_str: str,
    gids_str: str | None,
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    or_query = _build_or_tsquery(query_text)
    if not or_query:
        return []

    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

    sql = text("""
        SELECT
            c.id              AS chunk_id,
            c.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date,
            c.speakers,
            c.chunk_text,
            c.start_ms,
            c.end_ms,
            ts_rank_cd(c.search_vector, to_tsquery('simple', :or_query)) AS bm25_score
        FROM chunks c
        JOIN meetings m ON c.meeting_id = m.id
        WHERE c.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.search_vector @@ to_tsquery('simple', :or_query)
          AND (CAST(:gids AS text[]) IS NULL OR CAST(:gids AS text[]) && c.speaker_graph_ids)
          AND (CAST(:date_from AS timestamptz) IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (CAST(:date_to   AS timestamptz) IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
        ORDER BY bm25_score DESC
        LIMIT :top_k
    """)
    try:
        rows = await db.execute(sql, {
            "or_query": or_query,
            "ids": ids_str,
            "gids": gids_str,
            "date_from": date_from,
            "date_to": date_to,
            "top_k": _TOP_K,
        })
        return [_row_to_dict(r, float(r.bm25_score)) for r in rows]
    except Exception:
        # BM25 fails open — degrade to vector-only rather than fail the request.
        return []


def _rrf_merge(
    vector_results: list[dict],
    bm25_results: list[dict],
    top_k: int,
) -> list[dict]:
    """Reciprocal Rank Fusion: score(d) = Σ 1 / (RRF_K + rank).

    Merges two ranked lists, deduplicates by chunk_id, returns top_k by score.
    """
    scores: dict[str, float] = {}
    chunks: dict[str, dict] = {}

    for rank, item in enumerate(vector_results):
        cid = item["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        chunks[cid] = item

    for rank, item in enumerate(bm25_results):
        cid = item["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        if cid not in chunks:
            chunks[cid] = item

    ranked = sorted(scores, key=lambda c: scores[c], reverse=True)
    result = []
    for cid in ranked[:top_k]:
        item = chunks[cid].copy()
        item["similarity_score"] = round(scores[cid], 4)
        result.append(item)
    return result


def _row_to_dict(r, score: float) -> dict:
    """Translate a chunk row into the shape consumed by answer.py + chat sources.

    Includes legacy aliases (`speaker_name`, `timestamp_ms`) so the existing
    `_build_sources()` in chat.py keeps working without modification.
    """
    speakers = list(r.speakers or [])
    return {
        "source_type": "transcript",
        "chunk_id": str(r.chunk_id),
        "meeting_id": str(r.meeting_id),
        "meeting_title": r.meeting_title or "",
        "meeting_date": r.meeting_date,
        "speakers": speakers,
        "chunk_text": list(r.chunk_text or []),
        "start_ms": r.start_ms,
        "end_ms": r.end_ms,
        "similarity_score": score,
        # Legacy aliases for chat._build_sources backward compatibility.
        "speaker_name": ", ".join(speakers) if speakers else None,
        "timestamp_ms": r.start_ms,
    }
