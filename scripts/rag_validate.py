"""
Validation script: prints the full transcript then runs multiple questions
through the RAG pipeline and shows only the final answers.

Usage:
    python scripts/rag_validate.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        k, _, v = _line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Transcript ────────────────────────────────────────────────────────────────

MEETING_SUBJECT = "Q4 Engineering Planning — API Migration & Release Schedule"
MEETING_DATE    = "2026-04-10"
SPEAKERS        = ["Priya Nair", "James Okonkwo", "Sara Mendes", "Vikram Patel"]

TRANSCRIPT = [
    ("00:05", "Priya Nair",    "Good morning everyone. Today we need to finalise the API migration plan and lock down the Q4 release dates. Let's start with where we are on the REST to GraphQL migration."),
    ("00:19", "James Okonkwo", "Sure. So the core schema is done — about 85% coverage. The remaining 15% is the reporting endpoints. My estimate is two more sprints, so we are looking at end of May for full migration. The main risk is the legacy auth middleware which still depends on the old REST token format."),
    ("00:39", "Sara Mendes",   "I flagged that auth dependency in the last sprint review. If we don't resolve it before the migration cutover we risk breaking the mobile clients. I'd recommend we tackle it in Sprint 18 and not wait. Agreed. James, can you own the auth middleware refactor and target Sprint 18? We can't slip the mobile release."),
    ("01:11", "James Okonkwo", "Yes, I'll take it. I'll pair with Ravi on the token format work. We should be able to close it within the sprint."),
    ("01:21", "Vikram Patel",  "On the release schedule — we have a hard dependency on DevOps finishing the blue-green deployment setup before we can do a zero-downtime cutover. That was supposed to be done by April 25th. Is that still on track?"),
    ("01:41", "Sara Mendes",   "I checked with the DevOps team yesterday. They are on track for April 25th but they flagged a potential issue with the Kubernetes ingress config for the new API gateway. They need about a day of testing once the config is applied."),
    ("01:59", "Priya Nair",    "So realistically blue-green is ready by April 26th or 27th. That still gives us a buffer before the May 3rd soft launch. I'll confirm with DevOps directly this afternoon."),
    ("02:16", "Vikram Patel",  "There is also the performance testing we need to schedule. Last time we ran load tests on the staging environment the API gateway was handling about 1200 requests per second. With the GraphQL layer added we should re-validate that. I'd suggest a load test window the week of April 28th."),
    ("02:36", "James Okonkwo", "That timing works for me. I'll prepare the k6 test scripts and coordinate with Sara on the test scenarios. We should test both the query and mutation paths since they have different cache behaviour."),
    ("02:53", "Sara Mendes",   "Good point. I'll write up the mutation test cases — especially the ones that touch the order fulfilment service since that had latency issues in the last test cycle. Target is 95th percentile under 200 milliseconds."),
    ("03:11", "Priya Nair",    "Perfect. Let me summarise the action items. James owns the auth middleware refactor by end of Sprint 18. Sara is writing the mutation test cases with a 200ms P95 target. Vikram will coordinate the load test window for April 28th. I'll confirm blue-green readiness with DevOps today. Any blockers?"),
    ("03:31", "Vikram Patel",  "One more thing — we still have not decided on the deprecation notice timeline for the old REST API. Customers on the legacy API need at least 90 days notice. If our cutover is May 3rd we should send the deprecation notice by end of this week."),
    ("03:46", "Priya Nair",    "You are right. I'll draft the deprecation notice today and circulate it for review before end of day. Sara, can you review the customer-facing language?"),
    ("04:01", "Sara Mendes",   "Absolutely, send it over. I'll review by tomorrow morning."),
]

QUESTIONS = [
    "What are the action items and who owns them?",
    "What is the risk with the mobile release and how is it being addressed?",
    "When is the load test scheduled and what are the performance targets?",
    "What is the blue-green deployment status and when will it be ready?",
    "What is the deprecation notice plan for the old REST API?",
    "What did James say about the GraphQL migration completion timeline?",
]

SYNTHETIC_VTT = """\
WEBVTT

00:00:05.000 --> 00:00:18.000
<v Priya Nair>Good morning everyone. Today we need to finalise the API migration plan and lock down the Q4 release dates. Let's start with where we are on the REST to GraphQL migration.

00:00:19.000 --> 00:00:38.000
<v James Okonkwo>Sure. So the core schema is done — about 85% coverage. The remaining 15% is the reporting endpoints. My estimate is two more sprints, so we are looking at end of May for full migration. The main risk is the legacy auth middleware which still depends on the old REST token format.

00:00:39.000 --> 00:00:55.000
<v Sara Mendes>I flagged that auth dependency in the last sprint review. If we don't resolve it before the migration cutover we risk breaking the mobile clients. I'd recommend we tackle it in Sprint 18 and not wait.

00:00:56.000 --> 00:01:10.000
<v Priya Nair>Agreed. James, can you own the auth middleware refactor and target Sprint 18? We can't slip the mobile release.

00:01:11.000 --> 00:01:20.000
<v James Okonkwo>Yes, I'll take it. I'll pair with Ravi on the token format work. We should be able to close it within the sprint.

00:01:21.000 --> 00:01:40.000
<v Vikram Patel>On the release schedule — we have a hard dependency on DevOps finishing the blue-green deployment setup before we can do a zero-downtime cutover. That was supposed to be done by April 25th. Is that still on track?

00:01:41.000 --> 00:01:58.000
<v Sara Mendes>I checked with the DevOps team yesterday. They are on track for April 25th but they flagged a potential issue with the Kubernetes ingress config for the new API gateway. They need about a day of testing once the config is applied.

00:01:59.000 --> 00:02:15.000
<v Priya Nair>So realistically blue-green is ready by April 26th or 27th. That still gives us a buffer before the May 3rd soft launch. I'll confirm with DevOps directly this afternoon.

00:02:16.000 --> 00:02:35.000
<v Vikram Patel>There is also the performance testing we need to schedule. Last time we ran load tests on the staging environment the API gateway was handling about 1200 requests per second. With the GraphQL layer added we should re-validate that. I'd suggest a load test window the week of April 28th.

00:02:36.000 --> 00:02:52.000
<v James Okonkwo>That timing works for me. I'll prepare the k6 test scripts and coordinate with Sara on the test scenarios. We should test both the query and mutation paths since they have different cache behaviour.

00:02:53.000 --> 00:03:10.000
<v Sara Mendes>Good point. I'll write up the mutation test cases — especially the ones that touch the order fulfilment service since that had latency issues in the last test cycle. Target is 95th percentile under 200 milliseconds.

00:03:11.000 --> 00:03:30.000
<v Priya Nair>Perfect. Let me summarise the action items. James owns the auth middleware refactor by end of Sprint 18. Sara is writing the mutation test cases with a 200ms P95 target. Vikram will coordinate the load test window for April 28th. I'll confirm blue-green readiness with DevOps today. Any blockers?

00:03:31.000 --> 00:03:45.000
<v Vikram Patel>One more thing — we still have not decided on the deprecation notice timeline for the old REST API. Customers on the legacy API need at least 90 days notice. If our cutover is May 3rd we should send the deprecation notice by end of this week.

00:03:46.000 --> 00:04:00.000
<v Priya Nair>You are right. I'll draft the deprecation notice today and circulate it for review before end of day. Sara, can you review the customer-facing language?

00:04:01.000 --> 00:04:10.000
<v Sara Mendes>Absolutely, send it over. I'll review by tomorrow morning.
"""


# ── Pipeline setup (run once, reuse embeddings) ───────────────────────────────

import math

def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def _bm25_rank(chunks, query):
    terms  = set(query.lower().split())
    scores = [(i, sum(c.text.lower().split().count(t) for t in terms)) for i, c in enumerate(chunks)]
    hits   = sorted([(i, s) for i, s in scores if s > 0], key=lambda x: x[1], reverse=True)
    return {i: r + 1 for r, (i, _) in enumerate(hits)}

def _rrf_fuse(vec_ranks, bm25_ranks, pool, top_k=5, rrf_k=60):
    all_ids = set(vec_ranks) | set(bm25_ranks)
    scores  = {
        cid: 1.0 / (rrf_k + vec_ranks.get(cid, pool + 1))
              + 1.0 / (rrf_k + bm25_ranks.get(cid, pool + 1))
        for cid in all_ids
    }
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


async def run() -> None:
    import uuid
    from app.services.ingestion.vtt_parser    import parse_vtt
    from app.services.ingestion.chunker       import chunk_with_sentences, merge_speaker_turns
    from app.services.ingestion.contextualizer import build_contextual_text
    from app.services.ingestion.embedder      import embed_batch, embed_single
    from app.services.chat.answer             import generate_answer

    # ── Print transcript ──────────────────────────────────────────────────────
    print("=" * 72)
    print(f"  TRANSCRIPT: {MEETING_SUBJECT}")
    print(f"  Date: {MEETING_DATE}   Participants: {', '.join(SPEAKERS)}")
    print("=" * 72)
    for ts, speaker, text in TRANSCRIPT:
        print(f"\n  [{ts}] {speaker}")
        # Word-wrap at 68 chars
        words, line = text.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > 68:
                print(f"    {line}")
                line = w
            else:
                line = (line + " " + w).strip()
        if line:
            print(f"    {line}")
    print()

    # ── Prepare chunks + embeddings once ─────────────────────────────────────
    print("=" * 72)
    print("  Preparing chunks and embeddings (done once for all questions)...")
    print("=" * 72)

    segments   = parse_vtt(SYNTHETIC_VTT)
    merged     = merge_speaker_turns(segments)
    chunks     = chunk_with_sentences(merged)
    free_layer = [build_contextual_text(MEETING_SUBJECT, MEETING_DATE, SPEAKERS, c) for c in chunks]

    chunk_embeddings = await embed_batch(free_layer)
    print(f"  {len(chunks)} chunks embedded (dim={len(chunk_embeddings[0])})\n")

    dummy_id = uuid.uuid4()

    # ── Q&A ───────────────────────────────────────────────────────────────────
    print("=" * 72)
    print("  Q & A")
    print("=" * 72)

    for qi, question in enumerate(QUESTIONS, 1):
        # Embed query
        q_emb = await embed_single(question)

        # Cosine ranking
        cos_scores = sorted(
            [(i, _cosine(q_emb, emb)) for i, emb in enumerate(chunk_embeddings)],
            key=lambda x: x[1], reverse=True
        )
        vec_ranks = {idx: r + 1 for r, (idx, _) in enumerate(cos_scores)}
        cos_map   = dict(cos_scores)

        # BM25 + RRF
        bm25_ranks = _bm25_rank(chunks, question)
        fused      = _rrf_fuse(vec_ranks, bm25_ranks, pool=len(chunks))

        # Build handler result
        handler_result = [
            {
                "source_type":      "transcript",
                "meeting_id":       str(dummy_id),
                "meeting_title":    MEETING_SUBJECT,
                "meeting_date":     MEETING_DATE,
                "speaker_name":     chunks[idx].speaker,
                "text":             chunks[idx].text,
                "timestamp_ms":     chunks[idx].start_ms,
                "similarity_score": cos_map[idx],
            }
            for idx, _ in fused
        ]

        # Answer
        answer = await generate_answer(question, "SEARCH", handler_result, [])

        # Print
        print(f"\nQ{qi}: {question}")
        print("-" * 72)

        print("  Retrieved chunks:")
        for rank, (idx, rrf_score) in enumerate(fused, 1):
            c  = chunks[idx]
            ts = f"{c.start_ms // 1000 // 60:02d}:{c.start_ms // 1000 % 60:02d}"
            print(f"    [{rank}] [{ts}] {c.speaker}: {c.text[:80]}{'...' if len(c.text)>80 else ''}")

        print(f"\n  Answer:")
        for line in answer.splitlines():
            print(f"    {line}")
        print()

    print("=" * 72)
    print("  Done. Validate answers against the transcript above.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(run())
