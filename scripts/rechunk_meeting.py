"""
Re-run the full RAG ingestion pipeline on a meeting whose VTT is already in the DB.

Usage
-----
  # List all available meetings in the tenant DB
  python scripts/rechunk_meeting.py --db-url "postgresql+psycopg_async://..." --list

  # Re-ingest a specific meeting then ask a test question
  python scripts/rechunk_meeting.py \
      --db-url "postgresql+psycopg_async://..." \
      --meeting-id <uuid> \
      --question "What was decided about the Acme contract?"

  # Re-ingest ALL meetings that have a transcript
  python scripts/rechunk_meeting.py --db-url "postgresql+psycopg_async://..." --all

Environment variables required (Azure OpenAI)
----------------------------------------------
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_DEPLOYMENT_EMBEDDING   (e.g. text-embedding-3-small)
  AZURE_OPENAI_DEPLOYMENT_LLM         (e.g. gpt-4o)
  AZURE_OPENAI_DEPLOYMENT_LLM_MINI    (e.g. gpt-4o-mini, optional)

  # Other required settings (dummy values are fine for local DBs):
  AZURE_CLIENT_ID=local-test
  AZURE_CLIENT_SECRET=local-test
  AZURE_TENANT_ID=local-test
  CENTRAL_DB_URL=postgresql+psycopg2://unused:unused@localhost/unused
  TENANT_DB_USER=unused
  AZURE_KEYVAULT_URL=https://unused.vault.azure.net
  REDIS_URL=redis://localhost:6379
  WEBHOOK_BASE_URL=https://unused.example.com
  WEBHOOK_CLIENT_STATE=unused
  AZURE_TEXT_ANALYTICS_ENDPOINT=https://unused.cognitiveservices.azure.com
  AZURE_TEXT_ANALYTICS_KEY=unused
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure project root is in path when running as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_engine(db_url: str):
    """Build an async engine. Skips the sslmode check for local convenience."""
    return create_async_engine(
        db_url,
        pool_size=2,
        max_overflow=3,
        pool_pre_ping=True,
    )


async def list_meetings(db: AsyncSession) -> None:
    from app.db.tenant.models import Meeting, Transcript

    result = await db.execute(
        select(Meeting, Transcript)
        .join(Transcript, Transcript.meeting_id == Meeting.id, isouter=True)
        .order_by(Meeting.meeting_date.desc().nullslast())
    )
    rows = result.all()

    if not rows:
        print("No meetings found in this tenant DB.")
        return

    print(f"\n{'Meeting ID':<38}  {'Status':<10}  {'Has VTT':<8}  Subject")
    print("-" * 90)
    for meeting, transcript in rows:
        has_vtt = "YES" if transcript and transcript.raw_text else "no"
        subject = (meeting.meeting_subject or "(no subject)")[:50]
        print(f"{meeting.id!s:<38}  {meeting.status:<10}  {has_vtt:<8}  {subject}")
    print()


async def rechunk_single(
    db: AsyncSession,
    meeting_id: uuid.UUID,
    question: str | None,
) -> None:
    from app.db.tenant.models import Meeting, MeetingParticipant, Transcript, User
    from app.services.ingestion.pipeline import run_ingestion_pipeline

    # ── Load meeting ──────────────────────────────────────────────────────────
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        print(f"ERROR: Meeting {meeting_id} not found.")
        return

    # ── Load VTT from transcript ───────────────────────────────────────────────
    result = await db.execute(
        select(Transcript).where(Transcript.meeting_id == meeting_id)
    )
    transcript = result.scalar_one_or_none()
    if transcript is None or not transcript.raw_text:
        print(f"ERROR: No transcript / VTT content found for meeting {meeting_id}.")
        return

    print(f"\n{'='*60}")
    print(f"Meeting  : {meeting.meeting_subject or '(no subject)'}")
    print(f"ID       : {meeting_id}")
    print(f"Status   : {meeting.status}")
    print(f"VTT size : {len(transcript.raw_text):,} chars")
    print(f"{'='*60}")
    print("Running ingestion pipeline (rechunk → contextualize → embed)...")

    # ── Re-run ingestion pipeline ─────────────────────────────────────────────
    await run_ingestion_pipeline(
        meeting_id=meeting_id,
        vtt_content=transcript.raw_text,
        db=db,
        credits_per_minute=1,
    )
    await db.commit()
    print(f"✓ Ingestion complete — meeting.status={meeting.status}")

    # ── Optional: test chat ───────────────────────────────────────────────────
    if not question:
        return

    from app.db.tenant.models import MeetingParticipant
    from app.services.chat.orchestrator import handle_chat

    # Find any participant to use as the chat user (or create a dummy one).
    part_result = await db.execute(
        select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting_id).limit(1)
    )
    participant = part_result.scalar_one_or_none()

    if participant is None:
        print(
            "\n⚠  No participants found for this meeting — "
            "cannot test chat (RBAC requires a participant).\n"
            "   Add a participant row first, or use --skip-chat."
        )
        return

    user_id = participant.user_id
    print(f"\nAsking as user : {user_id}")
    print(f"Question       : {question}\n")

    answer, hits, session_id, query_type = await handle_chat(
        user_id=user_id,
        meeting_id=meeting_id,
        query=question,
        session_id=None,
        db=db,
    )
    await db.commit()

    print(f"Query type : {query_type}")
    print(f"Chunks hit : {len(hits)}")
    print(f"Session ID : {session_id}")
    print(f"\n--- Answer ---\n{answer}\n")

    if hits:
        print("Top 3 citations:")
        for i, h in enumerate(hits[:3], 1):
            print(f"  [{i}] [{h.speaker}] {h.text[:120]}...")


async def run(args: argparse.Namespace) -> None:
    engine = _make_engine(args.db_url)
    SessionLocal = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)

    async with SessionLocal() as db:
        if args.list:
            await list_meetings(db)
            return

        if args.all:
            from app.db.tenant.models import Transcript
            result = await db.execute(select(Transcript.meeting_id))
            meeting_ids = [row[0] for row in result.all()]
            print(f"Found {len(meeting_ids)} meetings with transcripts.")
            for mid in meeting_ids:
                await rechunk_single(db, mid, question=None)
            return

        if not args.meeting_id:
            print("ERROR: Provide --meeting-id <uuid>, --list, or --all.")
            sys.exit(1)

        await rechunk_single(
            db,
            uuid.UUID(args.meeting_id),
            question=args.question,
        )

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-run RAG ingestion on meetings whose VTT is already in the DB."
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help="Async tenant DB URL, e.g. postgresql+psycopg_async://user:pass@host/db",
    )
    parser.add_argument("--meeting-id", help="UUID of the meeting to rechunk")
    parser.add_argument("--question", help="Test question to ask after rechunking")
    parser.add_argument("--list", action="store_true", help="List all meetings and exit")
    parser.add_argument("--all", action="store_true", help="Re-ingest all meetings with transcripts")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
