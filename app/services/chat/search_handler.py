"""SEARCH route handler — hybrid BM25 + pgvector with Reciprocal Rank Fusion."""
from __future__ import annotations

import asyncio
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SIMILARITY_THRESHOLD = 0.60
_TOP_K = 10
_RRF_K = 60  # standard RRF constant — higher = smoother rank blending

# Common English stop words to exclude when building OR tsquery
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
    """
    Convert a natural language query into a to_tsquery OR expression.
    Uses 'simple' config (no stemming) to preserve proper nouns like names.
    Returns None if no significant terms remain after filtering stop words.
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
    """
    Hybrid retrieval: pgvector cosine similarity + PostgreSQL BM25 full-text search,
    merged via Reciprocal Rank Fusion. Each method contributes up to TOP_K results;
    RRF re-ranks the union and returns the best TOP_K overall.

    BM25 catches exact-match terms (names, acronyms, meeting titles).
    Vector search catches semantic matches (paraphrases, concepts).
    """
    if not authorized_meeting_ids:
        return []

    ids_str = "{" + ",".join(str(mid) for mid in authorized_meeting_ids) + "}"

    vector_results, bm25_results = await asyncio.gather(
        _vector_search(query_embedding, ids_str, filters, db),
        _bm25_search(query_text, ids_str, filters, db),
    )

    return _rrf_merge(vector_results, bm25_results, top_k=_TOP_K)


async def _vector_search(
    query_embedding: list[float],
    ids_str: str,
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    speaker  = filters.get("speaker")
    keyword  = filters.get("keyword")
    date_from = filters.get("date_from")
    date_to   = filters.get("date_to")

    sql = text("""
        SELECT
            c.id             AS chunk_id,
            t.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date,
            c.speaker        AS speaker_name,
            c.text,
            c.start_ms       AS timestamp_ms,
            1.0 - (c.embedding <=> CAST(:embedding AS vector)) AS similarity_score
        FROM chunks c
        JOIN transcripts t ON c.transcript_id = t.id
        JOIN meetings    m ON t.meeting_id    = m.id
        WHERE t.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.embedding IS NOT NULL
          AND 1.0 - (c.embedding <=> CAST(:embedding AS vector)) > :threshold
          AND (CAST(:speaker   AS text)        IS NULL OR c.speaker      ILIKE :speaker_pattern)
          AND (CAST(:keyword   AS text)        IS NULL OR c.text         ILIKE :keyword_pattern)
          AND (CAST(:date_from AS timestamptz) IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (CAST(:date_to   AS timestamptz) IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
        ORDER BY similarity_score DESC
        LIMIT :top_k
    """)
    rows = await db.execute(sql, {
        "embedding": embedding_str,
        "ids": ids_str,
        "threshold": _SIMILARITY_THRESHOLD,
        "speaker": speaker,
        "speaker_pattern": f"%{speaker}%" if speaker else None,
        "keyword": keyword,
        "keyword_pattern": f"%{keyword}%" if keyword else None,
        "date_from": date_from,
        "date_to": date_to,
        "top_k": _TOP_K,
    })
    return [_row_to_dict(r, float(r.similarity_score)) for r in rows]


async def _bm25_search(
    query_text: str,
    ids_str: str,
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    or_query = _build_or_tsquery(query_text)
    if not or_query:
        return []

    speaker   = filters.get("speaker")
    keyword   = filters.get("keyword")
    date_from = filters.get("date_from")
    date_to   = filters.get("date_to")

    # Uses 'simple' config so proper nouns (names, acronyms) are matched as-is.
    # OR logic ensures any significant term in the query matches relevant chunks.
    sql = text("""
        SELECT
            c.id             AS chunk_id,
            t.meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date,
            c.speaker        AS speaker_name,
            c.text,
            c.start_ms       AS timestamp_ms,
            ts_rank_cd(c.text_tsv, to_tsquery('simple', :or_query)) AS bm25_score
        FROM chunks c
        JOIN transcripts t ON c.transcript_id = t.id
        JOIN meetings    m ON t.meeting_id    = m.id
        WHERE t.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND c.text_tsv @@ to_tsquery('simple', :or_query)
          AND (CAST(:speaker   AS text)        IS NULL OR c.speaker      ILIKE :speaker_pattern)
          AND (CAST(:keyword   AS text)        IS NULL OR c.text         ILIKE :keyword_pattern)
          AND (CAST(:date_from AS timestamptz) IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (CAST(:date_to   AS timestamptz) IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
        ORDER BY bm25_score DESC
        LIMIT :top_k
    """)
    try:
        rows = await db.execute(sql, {
            "or_query": or_query,
            "ids": ids_str,
            "speaker": speaker,
            "speaker_pattern": f"%{speaker}%" if speaker else None,
            "keyword": keyword,
            "keyword_pattern": f"%{keyword}%" if keyword else None,
            "date_from": date_from,
            "date_to": date_to,
            "top_k": _TOP_K,
        })
        return [_row_to_dict(r, float(r.bm25_score)) for r in rows]
    except Exception:
        return []


def _rrf_merge(
    vector_results: list[dict],
    bm25_results: list[dict],
    top_k: int,
) -> list[dict]:
    """
    Reciprocal Rank Fusion: score(d) = Σ 1 / (RRF_K + rank).
    Merges two ranked lists, deduplicates by chunk_id, returns top_k by RRF score.
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
    return {
        "source_type": "transcript",
        "chunk_id": str(r.chunk_id),
        "meeting_id": str(r.meeting_id),
        "meeting_title": r.meeting_title or "",
        "meeting_date": r.meeting_date,
        "speaker_name": r.speaker_name,
        "timestamp_ms": r.timestamp_ms,
        "text": r.text,
        "similarity_score": score,
    }
