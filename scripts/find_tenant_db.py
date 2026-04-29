"""
Auto-discovers tenant DB URLs from the central DB, then inspects + chats.

Usage:
  python scripts/find_tenant_db.py           # list tenants + meetings
  python scripts/find_tenant_db.py --chat    # pick first tenant, run full RAG chat
  python scripts/find_tenant_db.py --chat --question "Who owned the action items?"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Load .env before importing anything from app/
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# ── Central DB ────────────────────────────────────────────────────────────────

async def get_tenants() -> list[dict]:
    central_url = os.environ["CENTRAL_DB_URL"].replace(
        "postgresql+psycopg://", "postgresql+psycopg_async://"
    ).replace(
        "postgresql://", "postgresql+psycopg_async://"
    )
    engine = create_async_engine(central_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, org_name, ms_tenant_id, db_host, plan, status FROM tenants")
        )
        rows = [dict(r._mapping) for r in result]
    await engine.dispose()
    return rows


def _tenant_url(db_host: str) -> str:
    user = os.environ.get("TENANT_DB_USER", "va_admin")
    password = os.environ.get("TENANT_DB_PASSWORD", "")
    if "://" in db_host:
        url = db_host
    else:
        url = f"postgresql+psycopg_async://{user}:{password}@pg-va-dev.postgres.database.azure.com:5432/{db_host}"
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


# ── Per-tenant inspection + chat ──────────────────────────────────────────────

async def inspect_tenant(tenant: dict, question: str | None) -> None:
    from app.db.tenant.models import Chunk as ChunkRow, Meeting, MeetingParticipant, Transcript

    db_url = _tenant_url(tenant["db_host"])

    print(f"\n{'='*70}")
    print(f"Tenant : {tenant['org_name']}  |  plan={tenant['plan']}  |  status={tenant['status']}")
    print(f"DB     : {tenant['db_host']}")
    print(f"{'='*70}")

    engine = create_async_engine(db_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)

    try:
        async with Session() as db:
            # ── List meetings ─────────────────────────────────────────────────
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
            all_rows = rows.all()

            if not all_rows:
                print("  (no meetings)")
                return

            print(f"\n{'ID':<38}  {'Status':<10}  {'Chunks':<7}  {'VTT lines':<10}  Subject")
            print("-" * 100)

            meetings_ready = []
            for meeting, transcript, chunk_count in all_rows:
                vtt_lines = (
                    len(transcript.raw_text.strip().splitlines())
                    if transcript and transcript.raw_text else 0
                )
                subject = (meeting.meeting_subject or "(no subject)")[:48]
                has_emb = ""
                if chunk_count > 0:
                    meetings_ready.append((meeting, transcript, chunk_count))
                    has_emb = " ✓"
                print(
                    f"{meeting.id!s:<38}  {meeting.status:<10}  "
                    f"{chunk_count:<7}  {vtt_lines:<10}  {subject}{has_emb}"
                )

            if not question or not meetings_ready:
                return

            # ── Pick first ingested meeting ───────────────────────────────────
            meeting, transcript, chunk_count = meetings_ready[0]
            print(f"\n→ Using: '{meeting.meeting_subject or '(no subject)'}' ({meeting.id})")

            # ── Show raw VTT ──────────────────────────────────────────────────
            if transcript and transcript.raw_text:
                lines = transcript.raw_text.strip().splitlines()
                print(f"\n--- Raw VTT ({len(lines)} lines) ---")
                for line in lines[:40]:
                    print(f"  {line}")
                if len(lines) > 40:
                    print(f"  ... ({len(lines) - 40} more lines)")

            # ── Show chunks ───────────────────────────────────────────────────
            chunk_rows = await db.execute(
                select(ChunkRow)
                .where(ChunkRow.transcript_id == transcript.id)
                .order_by(ChunkRow.chunk_index)
            )
            chunks = chunk_rows.scalars().all()

            print(f"\n--- Chunks ({len(chunks)}) ---")
            for c in chunks:
                emb = "✓" if c.embedding is not None else "✗ NO EMBEDDING"
                print(f"  [{c.chunk_index:02d}] [{c.speaker}] {emb}")
                print(f"        {c.text[:110]}")

            if not any(c.embedding is not None for c in chunks):
                print("\n⚠  No embeddings — re-run ingestion pipeline first.")
                return

            # ── RBAC: find a participant user ─────────────────────────────────
            part = await db.execute(
                select(MeetingParticipant)
                .where(MeetingParticipant.meeting_id == meeting.id)
                .limit(1)
            )
            participant = part.scalar_one_or_none()
            if participant is None:
                print("\n⚠  No participant row — cannot satisfy RBAC check.")
                return

            # ── Run the full RAG pipeline ─────────────────────────────────────
            await _run_chat(db, participant.user_id, question)

    except Exception as exc:
        print(f"\nERROR: {exc}")
        import traceback; traceback.print_exc()
    finally:
        await engine.dispose()


async def _run_chat(db, user_id, question: str) -> None:
    from app.services.chat.orchestrator import get_authorized_meeting_ids
    from app.services.chat.router import classify_query
    from app.services.chat.search_handler import handle_search
    from app.services.chat.structured_handler import handle_structured
    from app.services.chat.meta_handler import handle_meta
    from app.services.chat.hybrid_handler import handle_hybrid
    from app.services.chat.answer import generate_answer
    from app.services.ingestion.embedder import embed_single

    print(f"\n{'─'*70}")
    print(f"CHAT TEST")
    print(f"Question : {question}")
    print(f"{'─'*70}")

    # RBAC
    authorized_ids = await get_authorized_meeting_ids(user_id, db)
    if not authorized_ids:
        print("⚠  No authorized meetings for this user.")
        return
    print(f"Authorized meetings : {len(authorized_ids)}")

    # Classify
    classification = await classify_query(question)
    route = classification["route"]
    filters = classification["filters"]
    search_query = classification["search_query"]
    print(f"Route               : {route}")
    print(f"Search query        : {search_query!r}")
    active_filters = {k: v for k, v in filters.items() if v is not None}
    if active_filters:
        print(f"Filters             : {active_filters}")

    # Embed (skip for META)
    query_embedding: list[float] = []
    if route != "META":
        print("Embedding query...", end=" ", flush=True)
        query_embedding = await embed_single(search_query)
        print("done")

    # Dispatch
    fallthrough = False
    if route == "META":
        result = await handle_meta(authorized_ids, filters, db)
    elif route == "STRUCTURED":
        result, fell = await handle_structured(authorized_ids, filters, db)
        fallthrough = fell
        if fallthrough:
            print("STRUCTURED fell through → SEARCH")
            route = "SEARCH"
            result = await handle_search(query_embedding, search_query, authorized_ids, filters, db)
    elif route == "SEARCH":
        result = await handle_search(query_embedding, search_query, authorized_ids, filters, db)
    else:
        result = await handle_hybrid(query_embedding, search_query, authorized_ids, filters, db)

    print(f"Results retrieved   : {len(result)}")
    if fallthrough:
        print(f"Fallthrough         : True (STRUCTURED → SEARCH)")

    # Show retrieved context
    print(f"\n--- Retrieved context ---")
    for i, item in enumerate(result[:5], 1):
        stype = item.get("source_type", "?")
        title = item.get("meeting_title", "")
        if stype == "transcript":
            speaker = item.get("speaker_name", "?")
            score = item.get("similarity_score", 0)
            ts = item.get("timestamp_ms")
            ts_str = f" @{ts//1000//60:02d}:{ts//1000%60:02d}" if ts else ""
            print(f"  [{i}] transcript  score={score:.3f}  [{speaker}{ts_str}]")
            print(f"       {item.get('text', '')[:100]}")
        elif stype == "insights":
            print(f"  [{i}] insights    {title}")
            if item.get("summary"):
                text_val = item["summary"]
                if isinstance(text_val, dict):
                    text_val = text_val.get("text", "")
                print(f"       summary: {str(text_val)[:100]}")
        elif stype == "metadata":
            print(f"  [{i}] metadata    {title}  participants={item.get('participant_count')}")

    # Generate answer
    print("\nGenerating answer...", end=" ", flush=True)
    answer = await generate_answer(question, route, result, [])
    print("done")

    print(f"\n{'─'*70}")
    print(f"ANSWER ({route}):")
    print(f"{'─'*70}")
    print(answer)
    print(f"{'─'*70}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(chat: bool, question: str) -> None:
    print("Connecting to central DB...")
    try:
        tenants = await get_tenants()
    except Exception as exc:
        print(f"ERROR connecting to central DB: {exc}")
        return

    if not tenants:
        print("No tenants found.")
        return

    print(f"Found {len(tenants)} tenant(s).")
    for tenant in tenants:
        await inspect_tenant(tenant, question if chat else None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect tenant DB and test RAG chat pipeline")
    parser.add_argument("--chat", action="store_true", help="Run chat test on first meeting with chunks")
    parser.add_argument("--question", default="What was discussed in this meeting?")
    args = parser.parse_args()
    asyncio.run(run(args.chat, args.question))


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
