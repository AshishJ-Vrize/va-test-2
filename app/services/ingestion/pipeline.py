"""V2 ingestion pipeline — multi-turn chunks with hybrid-search columns.

Stages
------
 1. status='ingesting'                                 → meetings
 2. parse VTT
 3. compute transcript stats
 4. upsert transcript                                  → transcripts
 5. resolve VTT speakers against meeting_participants
 6. merge same-speaker turns + multi-turn chunking     (in memory)
 7. build search_text + embedding_input per chunk
 8. embed all inputs (batched Azure call)
 9. persist chunks (DELETE then INSERT)                → chunks
10. speaker_analytics with user_id resolved via graph_id → speaker_analytics
11. credit_usage                                       → credit_usage  (append-only)
12. meeting summary (built from raw segments)          → meeting_summaries  (non-fatal)
13. status='ready'                                     → meetings
14. insights generation                                → meeting_insights   (non-fatal)

On failure in 1-11 or 13 — meetings.status='failed' and re-raise.
This function never calls db.commit() — the caller owns the transaction.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant.models import (
    Chunk as ChunkRow,
    CreditUsage,
    Meeting,
    MeetingSummary,
    SpeakerAnalytic,
    Transcript,
    User,
)
from app.services.ingestion.chunker import chunk_segments, merge_speaker_turns
from app.services.ingestion.contextualizer import (
    build_embedding_input,
    build_search_text,
)
from app.services.ingestion.embedder import embed_batch, embed_single
from app.services.ingestion.speaker_resolver import (
    ResolvedSpeaker,
    build_speaker_resolution,
)
from app.services.ingestion.vtt_parser import VttSegment, parse_vtt

log = logging.getLogger(__name__)

# Bumped from 1 → 2 for the multi-turn chunk schema. Bump again whenever the
# embedding-input recipe or the chunking strategy changes; old chunks then
# become identifiable via WHERE embedding_version < :current.
EMBEDDING_VERSION = 2


async def run_ingestion_pipeline(
    meeting_id: uuid.UUID,
    vtt_content: str,
    db: AsyncSession,
    credits_per_minute: int,
) -> None:
    """Orchestrate the v2 ingestion pipeline for a single meeting.

    Public entry point for the ingestion service. Called by the ingest route
    handler and by Celery tasks in workers/tasks/ingestion.py. The caller
    owns the transaction boundary — this function only flushes.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} not found in tenant DB")

    # ── Step 1: mark in progress ──────────────────────────────────────────────
    meeting.status = "ingesting"
    await db.flush()
    log.info("Ingestion started for meeting %s", meeting_id)

    try:
        # ── Step 2: parse VTT ──────────────────────────────────────────────
        segments: list[VttSegment] = parse_vtt(vtt_content)
        if not segments:
            raise ValueError("VTT produced zero segments — transcript may be empty")

        # ── Step 3: transcript stats ───────────────────────────────────────
        full_text = " ".join(s.text for s in segments)
        word_count = len(full_text.split())
        language = "en"

        # ── Step 4: upsert transcript ──────────────────────────────────────
        result = await db.execute(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        )
        transcript = result.scalar_one_or_none()
        if transcript is None:
            transcript = Transcript(
                meeting_id=meeting_id,
                raw_text=vtt_content,
                language=language,
                word_count=word_count,
            )
            db.add(transcript)
        else:
            transcript.raw_text = vtt_content
            transcript.language = language
            transcript.word_count = word_count
        await db.flush()

        # ── Step 5: resolve VTT speakers to graph IDs ──────────────────────
        unique_vtt_speakers = list({s.speaker for s in segments})
        resolution = await build_speaker_resolution(
            meeting_id=meeting_id,
            vtt_speakers=unique_vtt_speakers,
            db=db,
        )

        # ── Step 6: merge same-speaker turns + multi-turn chunking ─────────
        merged = merge_speaker_turns(segments)
        chunks = chunk_segments(merged, resolution)
        if not chunks:
            raise ValueError("Chunker produced zero chunks")

        # ── Step 7: derive search_text and embedding_input per chunk ───────
        # chunk_context is currently always None — kept as parameter so the
        # contextualizer can be wired in later without changing this loop.
        search_texts = [build_search_text(c.chunk_text, None) for c in chunks]
        embed_inputs = [build_embedding_input(c.chunk_text, None) for c in chunks]

        # ── Step 8: embed all inputs in batch ──────────────────────────────
        embeddings = await embed_batch(embed_inputs)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedding count mismatch: {len(embeddings)} embeddings "
                f"for {len(chunks)} chunks"
            )

        # ── Step 9: persist chunks (DELETE then INSERT — idempotent) ───────
        await db.execute(
            delete(ChunkRow).where(ChunkRow.transcript_id == transcript.id)
        )
        await db.flush()

        for c, search_text, embedding in zip(chunks, search_texts, embeddings):
            db.add(ChunkRow(
                meeting_id=meeting_id,
                transcript_id=transcript.id,
                chunk_index=c.chunk_index,
                start_ms=c.start_ms,
                end_ms=c.end_ms,
                speakers=c.speakers,
                speaker_graph_ids=c.speaker_graph_ids,
                chunk_text=c.chunk_text,
                chunk_context=None,
                search_text=search_text,
                embedding=embedding,
                embedding_version=EMBEDDING_VERSION,
            ))
        await db.flush()
        log.info("Persisted %d chunks for meeting %s", len(chunks), meeting_id)

        # ── Step 10: speaker analytics (with user_id via graph_id) ─────────
        speaker_stats: dict[str, dict[str, int]] = {}
        for seg in segments:
            entry = speaker_stats.setdefault(
                seg.speaker, {"talk_ms": 0, "word_count": 0}
            )
            entry["talk_ms"] += max(0, seg.end_ms - seg.start_ms)
            entry["word_count"] += len(seg.text.split())

        gid_to_user_id = await _build_gid_to_user_id_map(resolution, db)

        await db.execute(
            delete(SpeakerAnalytic).where(SpeakerAnalytic.meeting_id == meeting_id)
        )
        await db.flush()

        for vtt_label, stats in speaker_stats.items():
            rs = resolution.get(vtt_label)
            user_id = gid_to_user_id.get(rs.graph_id) if rs and rs.graph_id else None
            db.add(SpeakerAnalytic(
                meeting_id=meeting_id,
                user_id=user_id,
                speaker_label=rs.n if rs else vtt_label,
                talk_time_seconds=max(1, stats["talk_ms"] // 1000),
                word_count=stats["word_count"],
            ))
        await db.flush()

        # ── Step 11: credit usage (append-only) ────────────────────────────
        duration_minutes = meeting.duration_minutes or 1
        credits_consumed = max(1, duration_minutes * credits_per_minute)
        db.add(CreditUsage(
            meeting_id=meeting_id,
            credits_consumed=credits_consumed,
            operation="ingestion",
        ))
        await db.flush()

        # ── Step 12: meeting summary (built from raw segments — non-fatal) ─
        try:
            await _upsert_meeting_summary(
                meeting_id=meeting_id,
                meeting_subject=meeting.meeting_subject or "",
                segments=segments,
                resolution=resolution,
                db=db,
            )
        except Exception:
            log.warning(
                "Meeting summary failed for %s — skipping",
                meeting_id,
                exc_info=True,
            )

        # ── Step 13: mark ready ────────────────────────────────────────────
        meeting.status = "ready"
        await db.flush()
        log.info(
            "Ingestion complete for meeting %s — %d chunks, %d credits",
            meeting_id, len(chunks), credits_consumed,
        )

        # ── Step 14: insights (non-fatal) ──────────────────────────────────
        try:
            from app.services.insights.generator import generate_insights_for_meeting
            await generate_insights_for_meeting(db=db, meeting_id=meeting_id)
        except Exception:
            log.warning(
                "Insight generation failed for %s — skipping",
                meeting_id,
                exc_info=True,
            )

    except Exception:
        meeting.status = "failed"
        await db.flush()
        log.exception("Ingestion failed for meeting %s", meeting_id)
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _build_gid_to_user_id_map(
    resolution: dict[str, ResolvedSpeaker],
    db: AsyncSession,
) -> dict[str, uuid.UUID]:
    """Look up user.id for every resolved graph_id in this meeting's speakers.

    Returns {} when no speakers resolved. SpeakerAnalytic.user_id stays NULL
    for unresolved or external speakers.
    """
    resolved_gids = {rs.graph_id for rs in resolution.values() if rs.graph_id}
    if not resolved_gids:
        return {}

    rows = await db.execute(
        select(User.id, User.graph_id).where(User.graph_id.in_(resolved_gids))
    )
    return {row.graph_id: row.id for row in rows}


# ── Meeting summary ───────────────────────────────────────────────────────────

_SUMMARY_SYSTEM_PROMPT = """\
You are generating Minutes of Meeting (MOM) from a business meeting transcript.
Return plain text in exactly this structure — no JSON, no markdown fences:

MEETING SUMMARY
<2-3 sentence overview of what was discussed and decided>

ATTENDEES
<comma-separated list of speakers who spoke>

AGENDA / TOPICS DISCUSSED
- <topic 1>
- <topic 2>
- <topic 3 ...>

KEY DECISIONS
- <decision>: <one-line rationale>

ACTION ITEMS
- [Owner] Task — Due: <date or TBD>

NEXT STEPS / FOLLOW-UPS
- <open question or deferred topic>

Rules:
- Use only information present in the transcript — never invent.
- Keep each bullet concise (one line).
- If a section has nothing, write "None noted."
- Include specific names, figures, dates, and product names when mentioned."""

_TOPIC_SYSTEM_PROMPT = """\
Extract 3-5 short topic labels (2-4 words each) from this meeting summary.
Return JSON: {"topics": ["topic 1", "topic 2", ...]}"""


async def _upsert_meeting_summary(
    meeting_id: uuid.UUID,
    meeting_subject: str,
    segments: list[VttSegment],
    resolution: dict[str, ResolvedSpeaker],
    db: AsyncSession,
) -> None:
    """Generate MOM summary, embed it, upsert into meeting_summaries.

    Built from raw VTT segments (not chunks) so the summary is independent
    of chunking strategy. Speaker labels in the prompt use resolved full
    names when available.
    """
    from app.services.ingestion.contextualizer import _get_client, _llm_deployment

    client = _get_client()
    deployment = _llm_deployment()

    # Render each segment as `<full_name>: <text>`. Truncate at 16 000 chars
    # (~4 000 words) — enough context for a useful summary, well below the
    # context window.
    lines: list[str] = []
    for seg in segments:
        rs = resolution.get(seg.speaker)
        name = rs.n if rs else (seg.speaker or "Unknown")
        lines.append(f"{name}: {seg.text}")
    transcript_text = "\n".join(lines)[:16000]

    summary_resp = await client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Meeting: {meeting_subject}\n\n{transcript_text}"},
        ],
        temperature=0,
        max_tokens=400,
    )
    summary_text = (summary_resp.choices[0].message.content or "").strip()
    if not summary_text:
        return

    topic_resp = await client.chat.completions.create(
        model=deployment,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _TOPIC_SYSTEM_PROMPT},
            {"role": "user", "content": summary_text},
        ],
        temperature=0,
        max_tokens=100,
    )
    raw_topics = json.loads(topic_resp.choices[0].message.content or "{}")
    topics: list[str] = raw_topics.get("topics", [])

    summary_embedding = await embed_single(summary_text)

    stmt = pg_insert(MeetingSummary).values(
        meeting_id=meeting_id,
        summary_text=summary_text,
        embedding=summary_embedding,
        topics=topics,
        generated_by=f"{deployment}@v{EMBEDDING_VERSION}",
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["meeting_id"],
        set_={
            "summary_text": stmt.excluded.summary_text,
            "embedding": stmt.excluded.embedding,
            "topics": stmt.excluded.topics,
            "generated_by": stmt.excluded.generated_by,
            "generated_at": stmt.excluded.generated_at,
        },
    )
    await db.execute(stmt)
    await db.flush()
    log.info("Meeting summary upserted for %s", meeting_id)
