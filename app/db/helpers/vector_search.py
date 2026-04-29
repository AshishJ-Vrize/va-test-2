"""
RBAC-aware hybrid retrieval for the chat RAG pipeline.

Two entry points:
  hybrid_chunk_search()   — pgvector cosine + BM25 fused with RRF, scoped to
                            caller-supplied meeting IDs (RBAC enforced by caller).
  cross_meeting_search()  — cosine search over meeting_summaries, scoped to
                            meetings where the requesting user is a participant.

The embedding strings are formatted as pgvector literals "[v1,v2,...]" and cast
with CAST(:param AS vector) to avoid the SQLAlchemy "::" parameter-parsing issue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ChunkHit:
    chunk_id: uuid.UUID
    meeting_id: uuid.UUID
    meeting_subject: str
    transcript_id: uuid.UUID
    speaker: str
    text: str
    start_ms: int
    end_ms: int
    score: float


@dataclass
class SummaryHit:
    meeting_id: uuid.UUID
    meeting_subject: str
    meeting_date: str | None
    summary_text: str
    topics: list[str] = field(default_factory=list)
    score: float = 0.0


# ── Public API ────────────────────────────────────────────────────────────────

async def hybrid_chunk_search(
    query_embedding: list[float],
    query_text: str,
    meeting_ids: list[uuid.UUID],
    db: AsyncSession,
    top_k: int = 15,
    rrf_k: int = 60,
) -> list[ChunkHit]:
    """
    Hybrid retrieval over chunks: pgvector cosine + Postgres BM25, fused with RRF.

    Runs two ranked candidate lists (each pool = top_k * 3), fuses scores in Python
    with Reciprocal Rank Fusion, then fetches the final top_k rows from the DB.

    meeting_ids must already be RBAC-filtered by the caller.
    """
    if not meeting_ids:
        return []

    pool = top_k * 3
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    meeting_ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"

    # ── Vector candidates (cosine distance — lower = more similar) ────────────
    vec_sql = text("""
        SELECT c.id AS chunk_id,
               ROW_NUMBER() OVER (
                   ORDER BY c.embedding <=> CAST(:embedding AS vector)
               ) AS rn
        FROM chunks c
        JOIN transcripts t ON c.transcript_id = t.id
        WHERE t.meeting_id = ANY(CAST(:meeting_ids AS uuid[]))
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:embedding AS vector)
        LIMIT :pool
    """)
    vec_result = await db.execute(
        vec_sql,
        {"embedding": embedding_str, "meeting_ids": meeting_ids_str, "pool": pool},
    )
    vec_ranks: dict[str, int] = {str(r.chunk_id): r.rn for r in vec_result}

    # ── BM25 candidates ────────────────────────────────────────────────────────
    bm25_sql = text("""
        SELECT c.id AS chunk_id,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(c.text_tsv, websearch_to_tsquery('english', :qtext)) DESC
               ) AS rn
        FROM chunks c
        JOIN transcripts t ON c.transcript_id = t.id
        WHERE t.meeting_id = ANY(CAST(:meeting_ids AS uuid[]))
          AND c.text_tsv @@ websearch_to_tsquery('english', :qtext)
        ORDER BY ts_rank_cd(c.text_tsv, websearch_to_tsquery('english', :qtext)) DESC
        LIMIT :pool
    """)
    bm25_result = await db.execute(
        bm25_sql,
        {"qtext": query_text, "meeting_ids": meeting_ids_str, "pool": pool},
    )
    bm25_ranks: dict[str, int] = {str(r.chunk_id): r.rn for r in bm25_result}

    # ── RRF fusion in Python ──────────────────────────────────────────────────
    ranked = _rrf_fuse(vec_ranks, bm25_ranks, rrf_k=rrf_k, pool=pool, top_k=top_k)
    if not ranked:
        return []

    score_map = dict(ranked)
    ranked_ids = [cid for cid, _ in ranked]

    # ── Fetch full chunk rows for the winner IDs ──────────────────────────────
    chunk_ids_str = "{" + ",".join(ranked_ids) + "}"
    fetch_sql = text("""
        SELECT c.id         AS chunk_id,
               t.meeting_id,
               m.meeting_subject,
               c.transcript_id,
               c.speaker,
               c.text,
               c.start_ms,
               c.end_ms
        FROM chunks c
        JOIN transcripts t ON c.transcript_id = t.id
        JOIN meetings m     ON t.meeting_id   = m.id
        WHERE c.id = ANY(CAST(:chunk_ids AS uuid[]))
    """)
    fetch_result = await db.execute(fetch_sql, {"chunk_ids": chunk_ids_str})
    row_map = {str(r.chunk_id): r for r in fetch_result}

    # Return in RRF-ranked order, skipping any IDs that disappeared (race condition).
    result: list[ChunkHit] = []
    for cid in ranked_ids:
        row = row_map.get(cid)
        if row is None:
            continue
        result.append(
            ChunkHit(
                chunk_id=row.chunk_id,
                meeting_id=row.meeting_id,
                meeting_subject=row.meeting_subject or "",
                transcript_id=row.transcript_id,
                speaker=row.speaker,
                text=row.text,
                start_ms=row.start_ms,
                end_ms=row.end_ms,
                score=score_map[cid],
            )
        )
    return result


async def cross_meeting_search(
    query_embedding: list[float],
    user_id: uuid.UUID,
    db: AsyncSession,
    top_k: int = 5,
) -> list[SummaryHit]:
    """
    Cosine similarity search over meeting_summaries.
    Automatically scoped to meetings where user_id appears in meeting_participants.
    """
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    sql = text("""
        SELECT ms.meeting_id,
               m.meeting_subject,
               m.meeting_date::text           AS meeting_date,
               ms.summary_text,
               ms.topics,
               1.0 - (ms.embedding <=> CAST(:embedding AS vector)) AS similarity
        FROM meeting_summaries ms
        JOIN meetings           m  ON ms.meeting_id = m.id
        JOIN meeting_participants mp ON m.id         = mp.meeting_id
        WHERE mp.user_id = CAST(:user_id AS uuid)
          AND ms.embedding IS NOT NULL
        ORDER BY ms.embedding <=> CAST(:embedding AS vector)
        LIMIT :top_k
    """)
    rows = await db.execute(
        sql,
        {"embedding": embedding_str, "user_id": str(user_id), "top_k": top_k},
    )
    return [
        SummaryHit(
            meeting_id=row.meeting_id,
            meeting_subject=row.meeting_subject or "",
            meeting_date=row.meeting_date,
            summary_text=row.summary_text,
            topics=row.topics or [],
            score=float(row.similarity),
        )
        for row in rows
    ]


# ── Private helpers ───────────────────────────────────────────────────────────

def _rrf_fuse(
    vec_ranks: dict[str, int],
    bm25_ranks: dict[str, int],
    rrf_k: int,
    pool: int,
    top_k: int,
) -> list[tuple[str, float]]:
    """
    Pure RRF fusion. IDs missing from one ranker get a penalty rank of pool+1.
    Returns list of (chunk_id_str, score) sorted by score descending.
    """
    all_ids = set(vec_ranks) | set(bm25_ranks)
    if not all_ids:
        return []
    scores = {
        cid: (
            1.0 / (rrf_k + vec_ranks.get(cid, pool + 1))
            + 1.0 / (rrf_k + bm25_ranks.get(cid, pool + 1))
        )
        for cid in all_ids
    }
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
