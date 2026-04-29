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
)
from app.services.ingestion.chunker import chunk_with_sentences, merge_speaker_turns
from app.services.ingestion.contextualizer import contextualize_chunks
from app.services.ingestion.embedder import embed_batch, embed_single
from app.services.ingestion.vtt_parser import VttSegment, parse_vtt

log = logging.getLogger(__name__)

# Increment this when the embedding model or contextualisation strategy changes.
# Old rows retain their version; scripts/backfill_embeddings.py migrates them lazily.
EMBEDDING_VERSION = 1


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

    Pipeline steps (in order)
    --------------------------
    1.  Set meeting.status = 'ingesting'
    2.  Parse the raw VTT string into ordered speaker segments.
    3.  Compute word count and language for the transcript record.
    4.  Upsert the transcripts row (supports re-ingestion of the same meeting).
    5.  Merge consecutive same-speaker turns, then split into sentence-aware
        chunks (≤ 250 words target, 40-word overlap, ≥ 20-word minimum).
    6.  Generate contextual embedding text for every chunk (one LLM call, batched).
        Falls back to free-layer context (meeting metadata + speaker label) if the
        LLM call fails — ingestion never blocks on contextualisation errors.
    7.  Embed all contextual texts via Azure OpenAI in batched API calls.
    8.  Delete old chunks (if any) and persist new ones with embeddings,
        contextual_text, and embedding_version.
    9.  Compute per-speaker talk time and word count → persist speaker_analytics.
    10. Record credit consumption in the append-only credit_usage ledger.
    11. Generate a meeting-level summary and embed it → upsert meeting_summaries.
        Used for cross-meeting RAG search.
    12. Set meeting.status = 'ready'.

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

        # ── Step 5: Merge turns and chunk (sentence-aware + overlap) ─────────
        merged = merge_speaker_turns(segments)
        chunks = chunk_with_sentences(merged)
        if not chunks:
            raise ValueError("Chunker produced zero chunks")

        # Ordered unique speakers — used for contextual embedding header.
        seen: set[str] = set()
        speakers: list[str] = []
        for c in chunks:
            if c.speaker not in seen:
                seen.add(c.speaker)
                speakers.append(c.speaker)

        meeting_subject = meeting.meeting_subject or ""
        meeting_date = (
            meeting.meeting_date.strftime("%Y-%m-%d")
            if meeting.meeting_date
            else ""
        )

        # ── Step 6: Generate contextual embedding texts ───────────────────────
        contextual_texts = await contextualize_chunks(
            meeting_subject=meeting_subject,
            meeting_date=meeting_date,
            speakers=speakers,
            chunks=chunks,
        )
        if len(contextual_texts) != len(chunks):
            raise ValueError(
                f"Contextualiser returned {len(contextual_texts)} texts "
                f"for {len(chunks)} chunks"
            )

        # ── Step 7: Embed contextual texts ────────────────────────────────────
        embeddings = await embed_batch(contextual_texts)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedding count mismatch: {len(embeddings)} embeddings "
                f"for {len(chunks)} chunks"
            )

        # ── Step 8: Persist chunks ────────────────────────────────────────────
        await db.execute(
            delete(ChunkRow).where(ChunkRow.transcript_id == transcript.id)
        )
        await db.flush()

        for chunk, ctx_text, embedding in zip(chunks, contextual_texts, embeddings):
            db.add(
                ChunkRow(
                    transcript_id=transcript.id,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    speaker=chunk.speaker,
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    contextual_text=ctx_text,
                    embedding=embedding,
                    embedding_version=EMBEDDING_VERSION,
                )
            )
        await db.flush()
        log.info("Persisted %d chunks for meeting %s", len(chunks), meeting_id)

        # ── Step 9: Speaker analytics ─────────────────────────────────────────
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

        # ── Step 10: Credit usage ──────────────────────────────────────────────
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

        # ── Step 11: Meeting summary for cross-meeting RAG ────────────────────
        try:
            await _upsert_meeting_summary(
                meeting_id=meeting_id,
                meeting_subject=meeting_subject,
                chunks=chunks,
                db=db,
            )
        except Exception:
            # Non-fatal: summary failure must not block ingestion completion.
            log.warning(
                "Meeting summary generation failed for %s — skipping",
                meeting_id,
                exc_info=True,
            )

        # ── Step 12: Mark ready ───────────────────────────────────────────────
        meeting.status = "ready"
        await db.flush()
        log.info(
            "Ingestion complete for meeting %s — %d chunks, %d credits consumed",
            meeting_id,
            len(chunks),
            credits_consumed,
        )

        # ── Step 13: Generate meeting insights (non-blocking) ────────────────
        try:
            from app.services.insights.generator import generate_insights_for_meeting
            await generate_insights_for_meeting(db=db, meeting_id=meeting_id)
        except Exception:
            log.warning(
                "Meeting insight generation failed for %s — skipping",
                meeting_id,
                exc_info=True,
            )

    except Exception:
        meeting.status = "failed"
        await db.flush()
        log.exception("Ingestion failed for meeting %s", meeting_id)
        raise


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
    chunks,
    db: AsyncSession,
) -> None:
    """
    Generate a meeting-level summary, embed it, and upsert into meeting_summaries.
    Used to power cross-meeting RAG search and meta queries.
    """
    from app.services.ingestion.contextualizer import _get_client, _llm_deployment

    client = _get_client()
    deployment = _llm_deployment()

    # Build a condensed transcript (up to 4000 words) for the summary prompt.
    transcript_text = "\n".join(
        f"{c.speaker}: {c.text}" for c in chunks
    )[:16000]

    # Generate summary.
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

    # Extract topics.
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

    # Embed summary.
    summary_embedding = await embed_single(summary_text)

    # Upsert — if a summary already exists (re-ingestion), replace it.
    stmt = pg_insert(MeetingSummary).values(
        meeting_id=meeting_id,
        summary_text=summary_text,
        embedding=summary_embedding,
        topics=topics,
        generated_by=f"{_llm_deployment()}@v{EMBEDDING_VERSION}",
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
