"""SEARCH route handler — pgvector cosine similarity with speaker/keyword/date filters."""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SIMILARITY_THRESHOLD = 0.75
_TOP_K = 10


async def handle_search(
    query_embedding: list[float],
    query_text: str,
    authorized_meeting_ids: list[uuid.UUID],
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    """
    Vector cosine similarity search over chunks with optional filters.
    Only returns chunks where similarity > 0.75. Top 10 results.
    """
    if not authorized_meeting_ids:
        return []

    ids_str = "{" + ",".join(str(mid) for mid in authorized_meeting_ids) + "}"
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    speaker = filters.get("speaker")
    keyword = filters.get("keyword")
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

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
          AND (:speaker IS NULL OR c.speaker ILIKE :speaker_pattern)
          AND (:keyword IS NULL OR c.text ILIKE :keyword_pattern)
          AND (:date_from IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (:date_to   IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
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

    return [
        {
            "source_type": "transcript",
            "meeting_id": str(r.meeting_id),
            "meeting_title": r.meeting_title or "",
            "meeting_date": r.meeting_date,
            "speaker_name": r.speaker_name,
            "timestamp_ms": r.timestamp_ms,
            "text": r.text,
            "similarity_score": float(r.similarity_score),
        }
        for r in rows
    ]
