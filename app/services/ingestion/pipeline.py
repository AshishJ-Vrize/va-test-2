from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

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


def run_ingestion_pipeline(
    meeting_id: uuid.UUID,
    vtt_content: str,
    db: Session,
    credits_per_minute: int,
) -> None:
    """
    Orchestrate the full ingestion pipeline for a single meeting.

    This is the only public entry point for the ingestion service.
    It is called by the Celery task in workers/tasks/ingestion.py after
    the raw VTT string has been fetched from the Microsoft Graph API.

    Pipeline steps (in order)
    --------------------------
    1. Set meeting.status = 'ingesting'  (signals other services to wait)
    2. Parse the raw VTT string into ordered speaker segments.
    3. Compute word count and language for the transcript record.
    4. Upsert the transcripts row (supports re-ingestion of the same meeting).
    5. Merge consecutive same-speaker turns, then split into ≤ 300-word chunks.
    6. Embed all chunks via Azure OpenAI in batched API calls.
    7. Delete old chunks (if any) and persist new ones with embeddings.
    8. Compute per-speaker talk time and word count → persist speaker_analytics.
    9. Record credit consumption in the append-only credit_usage ledger.
    10. Set meeting.status = 'ready'.

    On any failure
    --------------
    meeting.status is set to 'failed', the exception is logged, then re-raised
    so the Celery task can decide whether to retry or give up.

    Commit responsibility
    ---------------------
    This function only calls db.flush() — never db.commit().
    The caller (Celery task or route handler) owns the transaction boundary
    and must call db.commit() on success or db.rollback() on failure.

    Parameters
    ----------
    meeting_id       : UUID of the meetings row in the tenant DB.
    vtt_content      : Raw VTT string fetched from Graph API transcripts endpoint.
    db               : Active SQLAlchemy Session bound to the tenant database.
    credits_per_minute : Credit rate for this tenant's plan (fetched from central DB
                         by the Celery task before calling this function).
    """
    # ── Fetch the meeting row — fail fast if it doesn't exist ───────────────
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} not found in tenant DB")

    # ── Step 1: Mark ingestion in progress ──────────────────────────────────
    meeting.status = "ingesting"
    db.flush()
    log.info("Ingestion started for meeting %s", meeting_id)

    try:
        # ── Step 2: Parse VTT ────────────────────────────────────────────────
        # Converts the raw VTT string into a list of VttSegment objects.
        # Each segment has: speaker, text, start_ms, end_ms.
        # Empty cues and header blocks are discarded by the parser.
        segments: list[VttSegment] = parse_vtt(vtt_content)
        if not segments:
            raise ValueError("VTT produced zero segments — transcript may be empty")

        # ── Step 3: Compute transcript stats ─────────────────────────────────
        # Join all segment texts to count total words across the transcript.
        full_text = " ".join(s.text for s in segments)
        word_count = len(full_text.split())
        # Teams transcripts are always in English.
        # Update this when multilingual meeting support is added.
        language = "en"

        # ── Step 4: Upsert transcript row ────────────────────────────────────
        # The transcripts table has a UNIQUE constraint on meeting_id, so only
        # one transcript row exists per meeting.  On re-ingestion, overwrite it.
        transcript = (
            db.query(Transcript).filter(Transcript.meeting_id == meeting_id).first()
        )
        if transcript is None:
            transcript = Transcript(
                meeting_id=meeting_id,
                raw_text=vtt_content,   # full original VTT stored for auditability
                language=language,
                word_count=word_count,
            )
            db.add(transcript)
        else:
            # Re-ingestion: overwrite existing transcript with fresh content.
            transcript.raw_text = vtt_content
            transcript.language = language
            transcript.word_count = word_count

        db.flush()  # Flush here so transcript.id is assigned before chunk FKs reference it.

        # ── Step 5: Merge turns and chunk ────────────────────────────────────
        # merge_speaker_turns: joins consecutive same-speaker cues within 2 s gaps.
        # chunk_segments: splits merged turns into ≤ 300-word chunks.
        merged = merge_speaker_turns(segments)
        chunks = chunk_segments(merged)
        if not chunks:
            raise ValueError("Chunker produced zero chunks")

        # ── Step 6: Embed all chunks ─────────────────────────────────────────
        # embed_batch sends texts to Azure OpenAI text-embedding-3-small in
        # sub-batches of 16 and returns one 1536-dim vector per chunk.
        # Retries automatically on HTTP 429 (rate limit) up to 5 times.
        texts = [c.text for c in chunks]
        embeddings = embed_batch(texts)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedding count mismatch: {len(embeddings)} embeddings "
                f"for {len(chunks)} chunks"
            )

        # ── Step 7: Persist chunks ───────────────────────────────────────────
        # Delete old chunks first so re-ingestion doesn't create duplicates.
        # synchronize_session="fetch" keeps the SQLAlchemy identity map consistent.
        db.query(ChunkRow).filter(
            ChunkRow.transcript_id == transcript.id
        ).delete(synchronize_session="fetch")
        db.flush()

        for chunk, embedding in zip(chunks, embeddings):
            db.add(
                ChunkRow(
                    transcript_id=transcript.id,
                    chunk_index=chunk.chunk_index,   # ordering within the transcript
                    text=chunk.text,                 # spoken content for this chunk
                    speaker=chunk.speaker,           # primary speaker of this chunk
                    start_ms=chunk.start_ms,         # meeting-relative start time
                    end_ms=chunk.end_ms,             # meeting-relative end time
                    embedding=embedding,             # 1536-dim pgvector embedding
                )
            )
        db.flush()
        log.info("Persisted %d chunks for meeting %s", len(chunks), meeting_id)

        # ── Step 8: Speaker analytics ────────────────────────────────────────
        # Computed from the RAW (pre-merge) segments so talk_time_seconds reflects
        # actual mic-on airtime per speaker, not the boundaries of merged blocks.
        speaker_stats: dict[str, dict[str, int]] = {}
        for seg in segments:
            entry = speaker_stats.setdefault(
                seg.speaker, {"talk_ms": 0, "word_count": 0}
            )
            entry["talk_ms"] += max(0, seg.end_ms - seg.start_ms)
            entry["word_count"] += len(seg.text.split())

        # Delete old analytics before inserting fresh rows (same re-ingestion
        # safety as chunks above).
        db.query(SpeakerAnalytic).filter(
            SpeakerAnalytic.meeting_id == meeting_id
        ).delete(synchronize_session="fetch")
        db.flush()

        for speaker_label, stats in speaker_stats.items():
            db.add(
                SpeakerAnalytic(
                    meeting_id=meeting_id,
                    user_id=None,           # Nullable by design — linked to users.id
                                            # later by the speaker-diarization service.
                    speaker_label=speaker_label,
                    talk_time_seconds=max(1, stats["talk_ms"] // 1000),  # floor at 1 s
                    word_count=stats["word_count"],
                )
            )
        db.flush()

        # ── Step 9: Credit usage ─────────────────────────────────────────────
        # credit_usage is an append-only ledger — rows are never updated or deleted.
        # duration_minutes comes from the meetings row (set during meeting upsert
        # by the route/webhook handler before this pipeline is invoked).
        # Fallback to 1 minute if duration_minutes is NULL to avoid zero credits.
        duration_minutes = meeting.duration_minutes or 1
        credits_consumed = max(1, duration_minutes * credits_per_minute)
        db.add(
            CreditUsage(
                meeting_id=meeting_id,
                credits_consumed=credits_consumed,
                operation="ingestion",  # distinguishes from future operations
                                        # (e.g. "insights", "video_analysis")
            )
        )
        db.flush()

        # ── Step 10: Mark ready ──────────────────────────────────────────────
        meeting.status = "ready"
        db.flush()
        log.info(
            "Ingestion complete for meeting %s — %d chunks, %d credits consumed",
            meeting_id,
            len(chunks),
            credits_consumed,
        )

    except Exception:
        # Mark the meeting as failed so the UI can surface the error and the
        # Celery task can decide whether to retry.  The exception is re-raised
        # so the caller's transaction is rolled back.
        meeting.status = "failed"
        db.flush()
        log.exception("Ingestion failed for meeting %s", meeting_id)
        raise
