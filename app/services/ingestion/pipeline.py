from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant.models import (
    Chunk as ChunkRow,
    CreditUsage,
    Meeting,
    SpeakerAnalytic,
    Transcript,
)
from app.services.ingestion.chunker import chunk_segments, merge_speaker_turns
from app.services.ingestion.embedder import embed_batch
from app.services.ingestion.vtt_parser import VttSegment, parse_vtt

log = logging.getLogger(__name__)


async def run_ingestion_pipeline(
    meeting_id: uuid.UUID,
    vtt_content: str,
    db: AsyncSession,
    credits_per_minute: int,
) -> None:
    """
    Orchestrate the full ingestion pipeline for a single meeting.

    This is the only public entry point for the ingestion service.
    Called by the ingest route handler and by Celery tasks in workers/tasks/ingestion.py.

    Celery callers: wrap with asyncio.run(run_ingestion_pipeline(...)) or use
    an async Celery task since this function is async throughout.

    Pipeline steps (in order)
    --------------------------
    1. Set meeting.status = 'ingesting'
    2. Parse the raw VTT string into ordered speaker segments.
    3. Compute word count and language for the transcript record.
    4. Upsert the transcripts row (supports re-ingestion of the same meeting).
    5. Merge consecutive same-speaker turns, then split into ≤ 300-word chunks.
    6. Embed all chunks via Azure OpenAI in batched API calls.
    7. Delete old chunks (if any) and persist new ones with embeddings.
    8. Compute per-speaker talk time and word count → persist speaker_analytics.
    9. Record credit consumption in the append-only credit_usage ledger.
    10. Set meeting.status = 'ready'.

    On any failure: meeting.status is set to 'failed', exception is re-raised.
    This function never calls db.commit() — the caller owns the transaction boundary.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} not found in tenant DB")

    # ── Step 1: Mark ingestion in progress ──────────────────────────────────
    meeting.status = "ingesting"
    await db.flush()
    log.info("Ingestion started for meeting %s", meeting_id)

    try:
        # ── Step 2: Parse VTT ────────────────────────────────────────────────
        segments: list[VttSegment] = parse_vtt(vtt_content)
        if not segments:
            raise ValueError("VTT produced zero segments — transcript may be empty")

        # ── Step 3: Compute transcript stats ─────────────────────────────────
        full_text = " ".join(s.text for s in segments)
        word_count = len(full_text.split())
        language = "en"

        # ── Step 4: Upsert transcript row ────────────────────────────────────
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

        # ── Step 5: Merge turns and chunk ────────────────────────────────────
        merged = merge_speaker_turns(segments)
        chunks = chunk_segments(merged)
        if not chunks:
            raise ValueError("Chunker produced zero chunks")

        # ── Step 6: Embed all chunks ─────────────────────────────────────────
        texts = [c.text for c in chunks]
        embeddings = await embed_batch(texts)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedding count mismatch: {len(embeddings)} embeddings "
                f"for {len(chunks)} chunks"
            )

        # ── Step 7: Persist chunks ───────────────────────────────────────────
        await db.execute(
            delete(ChunkRow).where(ChunkRow.transcript_id == transcript.id)
        )
        await db.flush()

        for chunk, embedding in zip(chunks, embeddings):
            db.add(
                ChunkRow(
                    transcript_id=transcript.id,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    speaker=chunk.speaker,
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    embedding=embedding,
                )
            )
        await db.flush()
        log.info("Persisted %d chunks for meeting %s", len(chunks), meeting_id)

        # ── Step 8: Speaker analytics ────────────────────────────────────────
        speaker_stats: dict[str, dict[str, int]] = {}
        for seg in segments:
            entry = speaker_stats.setdefault(
                seg.speaker, {"talk_ms": 0, "word_count": 0}
            )
            entry["talk_ms"] += max(0, seg.end_ms - seg.start_ms)
            entry["word_count"] += len(seg.text.split())

        await db.execute(
            delete(SpeakerAnalytic).where(SpeakerAnalytic.meeting_id == meeting_id)
        )
        await db.flush()

        for speaker_label, stats in speaker_stats.items():
            db.add(
                SpeakerAnalytic(
                    meeting_id=meeting_id,
                    user_id=None,
                    speaker_label=speaker_label,
                    talk_time_seconds=max(1, stats["talk_ms"] // 1000),
                    word_count=stats["word_count"],
                )
            )
        await db.flush()

        # ── Step 9: Credit usage ─────────────────────────────────────────────
        duration_minutes = meeting.duration_minutes or 1
        credits_consumed = max(1, duration_minutes * credits_per_minute)
        db.add(
            CreditUsage(
                meeting_id=meeting_id,
                credits_consumed=credits_consumed,
                operation="ingestion",
            )
        )
        await db.flush()

        # ── Step 10: Mark ready ──────────────────────────────────────────────
        meeting.status = "ready"
        await db.flush()
        log.info(
            "Ingestion complete for meeting %s — %d chunks, %d credits consumed",
            meeting_id, len(chunks), credits_consumed,
        )

    except Exception:
        meeting.status = "failed"
        await db.flush()
        log.exception("Ingestion failed for meeting %s", meeting_id)
        raise
