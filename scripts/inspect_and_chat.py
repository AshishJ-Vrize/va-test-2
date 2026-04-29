"""
Inspect what's in the tenant DB and run a chat test against an ingested meeting.

Usage:
  python scripts/inspect_and_chat.py --db-url "postgresql+psycopg_async://..."

What it does:
  1. Shows all meetings + transcript line count + chunk count
  2. For any meeting that has chunks, asks a test question
  3. Prints the RAG answer + which chunks were cited
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def run(db_url: str, question: str) -> None:
    engine = create_async_engine(db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)

    async with Session() as db:
        from app.db.tenant.models import (
            Chunk as ChunkRow,
            Meeting,
            MeetingParticipant,
            Transcript,
        )

        # ── 1. Show what's in the DB ──────────────────────────────────────────
        rows = await db.execute(
            select(
                Meeting,
                Transcript,
                func.count(ChunkRow.id).label("chunk_count"),
            )
            .outerjoin(Transcript, Transcript.meeting_id == Meeting.id)
            .outerjoin(ChunkRow, ChunkRow.transcript_id == Transcript.id)
            .group_by(Meeting.id, Transcript.id)
            .order_by(Meeting.meeting_date.desc().nullslast())
        )
        rows = rows.all()

        print("\n=== Meetings in DB ===")
        print(f"{'ID':<38}  {'Status':<10}  {'Chunks':<7}  {'VTT lines':<10}  Subject")
        print("-" * 95)

        meetings_with_chunks = []
        for meeting, transcript, chunk_count in rows:
            vtt_lines = (
                len(transcript.raw_text.strip().splitlines())
                if transcript and transcript.raw_text
                else 0
            )
            subject = (meeting.meeting_subject or "(no subject)")[:45]
            print(
                f"{meeting.id!s:<38}  {meeting.status:<10}  {chunk_count:<7}  "
                f"{vtt_lines:<10}  {subject}"
            )
            if chunk_count > 0:
                meetings_with_chunks.append((meeting, transcript))

        if not meetings_with_chunks:
            print("\nNo meetings with chunks found. Run ingestion first.")
            await engine.dispose()
            return

        # ── 2. Pick the first meeting with chunks ─────────────────────────────
        meeting, transcript = meetings_with_chunks[0]
        print(f"\n→ Using meeting: '{meeting.meeting_subject or '(no subject)'}' ({meeting.id})")

        # ── 3. Show the raw transcript ────────────────────────────────────────
        if transcript and transcript.raw_text:
            print("\n=== Raw VTT content ===")
            for line in transcript.raw_text.strip().splitlines():
                print(f"  {line}")

        # ── 4. Show the chunks ────────────────────────────────────────────────
        chunk_result = await db.execute(
            select(ChunkRow)
            .where(ChunkRow.transcript_id == transcript.id)
            .order_by(ChunkRow.chunk_index)
        )
        chunks = chunk_result.scalars().all()

        print(f"\n=== Chunks ({len(chunks)} total) ===")
        for c in chunks:
            has_emb = "✓ embedded" if c.embedding is not None else "✗ no embedding"
            print(f"  [{c.chunk_index}] [{c.speaker}] {has_emb}")
            print(f"       {c.text[:100]}")

        # ── 5. Chat test ──────────────────────────────────────────────────────
        part_result = await db.execute(
            select(MeetingParticipant)
            .where(MeetingParticipant.meeting_id == meeting.id)
            .limit(1)
        )
        participant = part_result.scalar_one_or_none()

        if participant is None:
            print(
                "\n⚠  No participant row found — skipping chat test.\n"
                "   The orchestrator requires a MeetingParticipant row for RBAC."
            )
            await engine.dispose()
            return

        if not any(c.embedding is not None for c in chunks):
            print(
                "\n⚠  Chunks exist but have no embeddings — re-run ingestion pipeline first:\n"
                "   python scripts/rechunk_meeting.py --db-url '...' "
                f"--meeting-id {meeting.id} --question '...'"
            )
            await engine.dispose()
            return

        from app.services.chat.orchestrator import handle_chat

        print(f"\n=== Chat Test ===")
        print(f"Question: {question}\n")

        answer, hits, session_id, query_type = await handle_chat(
            user_id=participant.user_id,
            meeting_id=meeting.id,
            query=question,
            session_id=None,
            db=db,
        )
        await db.commit()

        print(f"Query type : {query_type}")
        print(f"Chunks hit : {len(hits)}")
        print(f"\nAnswer:\n{answer}")

        if hits:
            print("\nCited chunks:")
            for i, h in enumerate(hits[:3], 1):
                print(f"  [{i}] [{h.speaker}] {h.text[:120]}")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-url",
        required=True,
        help="e.g. postgresql+psycopg_async://user:pass@host:5432/dbname",
    )
    parser.add_argument(
        "--question",
        default="What was discussed in this meeting?",
        help="Question to ask the RAG chat",
    )
    args = parser.parse_args()
    asyncio.run(run(args.db_url, args.question))


if __name__ == "__main__":
    main()
