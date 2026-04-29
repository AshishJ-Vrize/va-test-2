"""
End-to-end RAG pipeline demo — no DB required.

Runs a synthetic meeting transcript through every stage of the pipeline
and prints the real outputs at each step.

Usage:
    python scripts/rag_e2e_demo.py
    python scripts/rag_e2e_demo.py --question "Who is responsible for the API migration?"
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load .env before importing any app code
_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        k, _, v = _line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Synthetic meeting transcript (WebVTT format) ──────────────────────────────

MEETING_SUBJECT = "Q4 Engineering Planning — API Migration & Release Schedule"
MEETING_DATE = "2026-04-10"
SPEAKERS = ["Priya Nair", "James Okonkwo", "Sara Mendes", "Vikram Patel"]

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def separator(title: str, width: int = 72) -> None:
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _rrf_fuse(
    vec_ranks: dict[int, int],
    bm25_ranks: dict[int, int],
    rrf_k: int = 60,
    pool: int = 45,
    top_k: int = 5,
) -> list[tuple[int, float]]:
    all_ids = set(vec_ranks) | set(bm25_ranks)
    scores = {
        cid: (
            1.0 / (rrf_k + vec_ranks.get(cid, pool + 1))
            + 1.0 / (rrf_k + bm25_ranks.get(cid, pool + 1))
        )
        for cid in all_ids
    }
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


def _bm25_rank(chunks, query_text: str) -> dict[int, int]:
    """Rough BM25 approximation — count query-term hits per chunk."""
    terms = set(query_text.lower().split())
    scores = []
    for i, c in enumerate(chunks):
        words = c.text.lower().split()
        tf = sum(words.count(t) for t in terms)
        scores.append((i, tf))
    # Only chunks with at least one hit get a real rank
    hit_chunks = [(i, s) for i, s in scores if s > 0]
    hit_chunks.sort(key=lambda x: x[1], reverse=True)
    return {i: rank + 1 for rank, (i, _) in enumerate(hit_chunks)}


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(question: str) -> None:

    # ── Stage 1: Parse VTT ────────────────────────────────────────────────────
    separator("STAGE 1 · VTT PARSING")
    from app.services.ingestion.vtt_parser import parse_vtt
    segments = parse_vtt(SYNTHETIC_VTT)
    print(f"Parsed {len(segments)} VTT segments\n")
    for seg in segments:
        ts = f"{seg.start_ms // 1000 // 60:02d}:{seg.start_ms // 1000 % 60:02d}"
        print(f"  [{ts}] [{seg.speaker}]  {seg.text[:80]}")

    # ── Stage 2: Merge + Chunk ────────────────────────────────────────────────
    separator("STAGE 2 · MERGE SPEAKER TURNS + SENTENCE-AWARE CHUNKING")
    from app.services.ingestion.chunker import chunk_with_sentences, merge_speaker_turns
    merged = merge_speaker_turns(segments)
    chunks = chunk_with_sentences(merged)
    print(f"Merged into {len(merged)} turns -> split into {len(chunks)} chunks\n")
    for c in chunks:
        ts = f"{c.start_ms // 1000 // 60:02d}:{c.start_ms // 1000 % 60:02d}"
        wc = len(c.text.split())
        print(f"  [chunk {c.chunk_index:02d}] [{ts}] [{c.speaker}] {wc} words")
        print(f"           {c.text[:100]}{'...' if len(c.text) > 100 else ''}")

    # ── Stage 3: Contextual Text (free-layer, no LLM) ─────────────────────────
    separator("STAGE 3 - CONTEXTUAL EMBEDDING TEXT (free-layer, no LLM cost)")
    from app.services.ingestion.contextualizer import build_contextual_text
    free_layer = [
        build_contextual_text(MEETING_SUBJECT, MEETING_DATE, SPEAKERS, c)
        for c in chunks
    ]
    print("Each chunk gets a metadata-enriched string before embedding:\n")
    for i, (c, ctx) in enumerate(zip(chunks, free_layer)):
        print(f"  [chunk {c.chunk_index:02d}]")
        print(f"    RAW  : {c.text[:90]}")
        print(f"    CTX  : {ctx[:140]}")
        print()

    # ── Stage 4: Embed (real Azure OpenAI) ───────────────────────────────────
    separator("STAGE 4 · EMBEDDING via Azure OpenAI")
    from app.services.ingestion.embedder import embed_batch, embed_single
    try:
        print(f"Embedding {len(chunks)} contextual texts + 1 query vector...")
        chunk_embeddings = await embed_batch(free_layer)
        query_embedding = await embed_single(question)
        print(f"[OK] Got {len(chunk_embeddings)} chunk embeddings, dim={len(chunk_embeddings[0])}")
        print(f"[OK] Got query embedding, dim={len(query_embedding)}")
        embedding_ok = True
    except Exception as e:
        print(f"[FAIL] Embedding failed: {e}")
        print("  (Azure OpenAI endpoint may not be reachable - using random vectors for demo)")
        import random
        random.seed(42)
        chunk_embeddings = [[random.gauss(0, 1) for _ in range(8)] for _ in chunks]
        query_embedding = [random.gauss(0, 1) for _ in range(8)]
        embedding_ok = False

    # ── Stage 5: Retrieval — Hybrid (cosine + BM25 → RRF) ────────────────────
    separator("STAGE 5 - RETRIEVAL: Hybrid cosine + BM25 fused with RRF")
    print(f"Query: \"{question}\"\n")

    # Cosine similarity ranking
    cos_scores = [
        (i, _cosine(query_embedding, emb))
        for i, emb in enumerate(chunk_embeddings)
    ]
    cos_scores.sort(key=lambda x: x[1], reverse=True)
    vec_ranks = {idx: rank + 1 for rank, (idx, _) in enumerate(cos_scores)}
    cos_map = dict(cos_scores)

    # BM25 term-frequency ranking
    bm25_ranks = _bm25_rank(chunks, question)

    print("  Vector (cosine) ranks:")
    for rank, (idx, score) in enumerate(cos_scores[:5], 1):
        bm25_r = bm25_ranks.get(idx, "-")
        print(f"    #{rank}  chunk {chunks[idx].chunk_index:02d}  cos={score:.4f}  bm25_rank={bm25_r}"
              f"  [{chunks[idx].speaker}]  {chunks[idx].text[:60]}...")

    print(f"\n  BM25 hits ({len(bm25_ranks)} chunks matched query terms):")
    for rank, (idx, r) in enumerate(sorted(bm25_ranks.items(), key=lambda x: x[1])[:5], 1):
        print(f"    #{r}  chunk {chunks[idx].chunk_index:02d}  [{chunks[idx].speaker}]  "
              f"{chunks[idx].text[:60]}...")

    # RRF fusion
    fused = _rrf_fuse(vec_ranks, bm25_ranks, top_k=5)
    print(f"\n  RRF-fused top-5:")
    for rank, (idx, score) in enumerate(fused, 1):
        c = chunks[idx]
        print(f"    #{rank}  chunk {c.chunk_index:02d}  rrf={score:.5f}"
              f"  vec=#{vec_ranks[idx]}  bm25=#{bm25_ranks.get(idx, 'N/A')}"
              f"  [{c.speaker}]")
        print(f"         {c.text[:90]}{'...' if len(c.text) > 90 else ''}")

    # ── Stage 6: Format handler_result (as SEARCH handler would) ─────────────
    separator("STAGE 6 · HANDLER RESULT (SEARCH route format)")
    import uuid
    dummy_meeting_id = uuid.uuid4()
    handler_result = []
    for idx, rrf_score in fused:
        c = chunks[idx]
        handler_result.append({
            "source_type": "transcript",
            "meeting_id": str(dummy_meeting_id),
            "meeting_title": MEETING_SUBJECT,
            "meeting_date": MEETING_DATE,
            "speaker_name": c.speaker,
            "text": c.text,
            "timestamp_ms": c.start_ms,
            "similarity_score": cos_map[idx],
        })

    for i, item in enumerate(handler_result, 1):
        ts = item["timestamp_ms"] // 1000
        print(f"  [{i}] {item['speaker_name']}  @{ts // 60:02d}:{ts % 60:02d}"
              f"  score={item['similarity_score']:.4f}")
        print(f"       {item['text'][:100]}{'...' if len(item['text']) > 100 else ''}")

    # ── Stage 7: Context string sent to LLM ──────────────────────────────────
    separator("STAGE 7 · CONTEXT STRING SENT TO LLM")
    from app.services.chat.answer import _build_context
    context_str = _build_context(handler_result, "SEARCH")
    print(context_str)

    # ── Stage 8: Generate Answer (real Azure OpenAI) ──────────────────────────
    separator("STAGE 8 · FINAL ANSWER — Azure OpenAI GPT-4o")
    print(f"Question: {question}\n")
    print("Calling LLM...", flush=True)
    try:
        from app.services.chat.answer import generate_answer
        answer = await generate_answer(question, "SEARCH", handler_result, [])
        print(f"\n{'─' * 72}")
        print("ANSWER:")
        print(f"{'─' * 72}")
        print(answer)
        print(f"{'─' * 72}")
    except Exception as e:
        print(f"✗ LLM call failed: {e}")

    # ── Summary stats ─────────────────────────────────────────────────────────
    separator("SUMMARY")
    print(f"  Transcript      : {len(segments)} VTT segments")
    print(f"  After merge     : {len(merged)} speaker turns")
    print(f"  Chunks          : {len(chunks)} sentence-aware chunks")
    print(f"  Embeddings      : {'real (Azure OpenAI)' if embedding_ok else 'random (demo fallback)'}")
    print(f"  Retrieval       : Cosine + BM25 → RRF top-5")
    print(f"  Route           : SEARCH")
    print(f"  Question        : {question}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--question",
        default="What are the action items and who owns them?",
    )
    args = parser.parse_args()
    asyncio.run(run(args.question))


if __name__ == "__main__":
    main()
