"""
Cross-meeting RAG demo - Q&A across 3 different meetings.

Shows how the HYBRID route pulls context from multiple meetings,
fuses insights + transcript chunks, and answers cross-meeting questions.

Usage:
    python scripts/rag_multi_meeting.py
"""
from __future__ import annotations

import asyncio
import json
import math
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

# ── 3 Synthetic Meetings ──────────────────────────────────────────────────────

MEETINGS = [
    {
        "id": "meeting-1",
        "subject": "Q4 API Migration Planning",
        "date": "2026-04-10",
        "speakers": ["Priya Nair", "James Okonkwo", "Sara Mendes", "Vikram Patel"],
        "vtt": """\
WEBVTT

00:00:05.000 --> 00:00:38.000
<v James Okonkwo>The core schema is done about 85% coverage. The remaining 15% is the reporting endpoints. We are looking at end of May for full migration. The main risk is the legacy auth middleware which still depends on the old REST token format.

00:00:39.000 --> 00:00:55.000
<v Sara Mendes>I flagged that auth dependency in the last sprint review. If we don't resolve it before the migration cutover we risk breaking the mobile clients. I'd recommend we tackle it in Sprint 18.

00:00:56.000 --> 00:01:10.000
<v Priya Nair>Agreed. James, can you own the auth middleware refactor and target Sprint 18? We can't slip the mobile release.

00:01:11.000 --> 00:01:20.000
<v James Okonkwo>Yes I'll take it. I'll pair with Ravi on the token format work. We should be able to close it within the sprint.

00:01:21.000 --> 00:01:40.000
<v Vikram Patel>On the release schedule we have a hard dependency on DevOps finishing the blue-green deployment setup by April 25th. We need zero-downtime cutover.

00:01:41.000 --> 00:02:00.000
<v Sara Mendes>DevOps is on track for April 25th but they flagged a Kubernetes ingress config issue for the new API gateway. They need a day of testing once the config is applied.

00:02:01.000 --> 00:02:20.000
<v Priya Nair>So blue-green is ready by April 26th or 27th. That still gives us a buffer before the May 3rd soft launch. James owns the auth middleware refactor by Sprint 18. Sara writes mutation test cases with 200ms P95. Vikram coordinates load tests April 28th.

00:02:21.000 --> 00:02:40.000
<v Vikram Patel>We also need to send the deprecation notice for the old REST API by end of this week. Customers need 90 days notice before the May 3rd cutover.

00:02:41.000 --> 00:03:00.000
<v Priya Nair>You are right. I'll draft the deprecation notice today and circulate for review. Sara can you review the customer-facing language?

00:03:01.000 --> 00:03:10.000
<v Sara Mendes>Absolutely, I'll review by tomorrow morning.
""",
    },
    {
        "id": "meeting-2",
        "subject": "Sprint 18 Kickoff - Auth Middleware Refactor",
        "date": "2026-04-14",
        "speakers": ["James Okonkwo", "Ravi Shankar", "Sara Mendes", "Priya Nair"],
        "vtt": """\
WEBVTT

00:00:05.000 --> 00:00:30.000
<v James Okonkwo>So the plan for the auth middleware refactor is to decouple it from the REST token format entirely. We'll introduce a token adapter layer that handles both old and new formats during the transition period.

00:00:31.000 --> 00:00:55.000
<v Ravi Shankar>I've been looking at the token schema. The old format uses a flat JWT structure while the new GraphQL tokens carry nested claims. I estimate about 3 days to write the adapter and 2 days for unit tests.

00:00:56.000 --> 00:01:15.000
<v James Okonkwo>Good. I'll handle the middleware integration while Ravi builds the adapter. We should have a working prototype by Wednesday. The key risk is the mobile SDK still hardcodes the old token field names.

00:01:16.000 --> 00:01:35.000
<v Sara Mendes>That mobile SDK issue is a blocker. If we change the token structure before the SDK is updated we'll break all mobile users. I'll reach out to the mobile team today and get them to push an SDK update in parallel.

00:01:36.000 --> 00:01:55.000
<v Priya Nair>Good catch Sara. That dependency needs to be tracked. James let's make the middleware release conditional on the mobile SDK update shipping first. What is the mobile team's timeline?

00:01:56.000 --> 00:02:15.000
<v Sara Mendes>I'll confirm today but my estimate is they can ship the SDK update within 5 days. So by April 19th. That still keeps us inside the Sprint 18 window.

00:02:16.000 --> 00:02:35.000
<v James Okonkwo>Works for me. I'll have the middleware prototype ready by Wednesday April 16th. Ravi finishes the adapter by Thursday April 17th. We review together Friday April 18th. If the SDK ships April 19th we are good to merge.

00:02:36.000 --> 00:02:55.000
<v Ravi Shankar>One more thing - we need to update the API documentation to reflect the new token structure. Otherwise developers integrating with GraphQL will be confused. I can write the first draft of the token migration guide.

00:02:56.000 --> 00:03:05.000
<v Priya Nair>Perfect. Ravi owns the token migration guide. Sara owns the mobile SDK coordination. James owns the middleware integration. Let's sync again on Friday.
""",
    },
    {
        "id": "meeting-3",
        "subject": "Customer Success Review - API Deprecation Impact",
        "date": "2026-04-17",
        "speakers": ["Anita Desai", "Priya Nair", "Tom Briggs", "Sara Mendes"],
        "vtt": """\
WEBVTT

00:00:05.000 --> 00:00:30.000
<v Anita Desai>We've had 12 customers reach out since the deprecation notice went out. Most are asking for a migration guide. Three enterprise customers - Acme Corp, FinServ Ltd, and RetailMax - are saying 90 days is not enough time. They want a 6-month extension.

00:00:31.000 --> 00:00:55.000
<v Tom Briggs>RetailMax specifically said they have 47 internal integrations that all need updating. Their engineering team is small and can't absorb the migration load alongside their own product roadmap. They're threatening to churn if we don't extend.

00:00:56.000 --> 00:01:15.000
<v Priya Nair>We can't move the cutover date without engineering sign-off. The May 3rd date is tied to the infrastructure contract renewal. But we can consider a compatibility layer that keeps the old REST endpoints alive in read-only mode for 6 more months.

00:01:16.000 --> 00:01:35.000
<v Sara Mendes>A read-only compatibility layer is technically feasible. It would add about 2 weeks of work for the API team. The risk is that customers treat it as permanent and never actually migrate.

00:01:36.000 --> 00:01:55.000
<v Anita Desai>What if we offer premium migration support? White-glove onboarding sessions for the enterprise customers who are struggling. That could accelerate their timelines without us needing to delay the cutover.

00:01:56.000 --> 00:02:15.000
<v Tom Briggs>I like that approach. Acme Corp already has a dedicated CSM so that would be easy to arrange. RetailMax and FinServ Ltd would need dedicated engineering support hours from our side though.

00:02:16.000 --> 00:02:35.000
<v Priya Nair>Let's do both - a read-only compatibility layer through October 2026 AND premium migration support for the three enterprise customers. Anita can you draft the outreach plan by Monday?

00:02:36.000 --> 00:02:55.000
<v Anita Desai>Yes I'll have it ready by Monday April 20th. I'll also put together a migration FAQ that we can share with all 12 customers who reached out.

00:02:56.000 --> 00:03:10.000
<v Sara Mendes>I'll coordinate with the API team to scope the compatibility layer work this week. We should know by Friday whether the 2-week estimate holds.
""",
    },
]

CROSS_MEETING_QUESTIONS = [
    "What is the current status of the auth middleware refactor?",
    "What are all the action items across all meetings and who owns them?",
    "What are the risks that could delay the May 3rd launch?",
    "What is the customer situation with the API deprecation?",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _bm25_score(text: str, query: str) -> int:
    terms = set(query.lower().split())
    words = text.lower().split()
    return sum(words.count(t) for t in terms)


def _rrf_fuse(vec_ranks, bm25_ranks, pool, top_k=6, rrf_k=60):
    all_ids = set(vec_ranks) | set(bm25_ranks)
    scores = {
        cid: 1.0 / (rrf_k + vec_ranks.get(cid, pool + 1))
              + 1.0 / (rrf_k + bm25_ranks.get(cid, pool + 1))
        for cid in all_ids
    }
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    from app.services.ingestion.vtt_parser import parse_vtt
    from app.services.ingestion.chunker import chunk_with_sentences, merge_speaker_turns
    from app.services.ingestion.contextualizer import build_contextual_text, _get_client, _llm_deployment
    from app.services.ingestion.embedder import embed_batch, embed_single
    from app.services.insights.prompts import INSIGHT_SYSTEM_PROMPT
    from app.services.insights.parser import parse_insights
    from app.services.chat.answer import generate_answer

    client = _get_client()
    deployment = _llm_deployment()

    # ── Step 1: Ingest all 3 meetings ─────────────────────────────────────────
    print("=" * 72)
    print("  INGESTING 3 MEETINGS")
    print("=" * 72)

    all_chunks     = []   # list of (meeting_meta, chunk)
    all_embeddings = []   # parallel to all_chunks
    all_insights   = []   # one dict per meeting
    all_summaries  = []   # one summary string per meeting

    for mtg in MEETINGS:
        segments = parse_vtt(mtg["vtt"])
        merged   = merge_speaker_turns(segments)
        chunks   = chunk_with_sentences(merged)

        free_layer = [
            build_contextual_text(mtg["subject"], mtg["date"], mtg["speakers"], c)
            for c in chunks
        ]
        embeddings = await embed_batch(free_layer)

        print(f"\n  [{mtg['id']}] {mtg['subject']}")
        print(f"   Date: {mtg['date']}  |  {len(segments)} segments -> {len(chunks)} chunks  |  {len(embeddings[0])}-dim embeddings")

        for c, emb in zip(chunks, embeddings):
            all_chunks.append({"meeting": mtg, "chunk": c})
            all_embeddings.append(emb)

        # Generate insights
        transcript_text = "\n".join(
            f"{seg.speaker}: {seg.text}" for seg in segments
        )
        insight_resp = await client.chat.completions.create(
            model=deployment,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Meeting: {mtg['subject']}\n\nTranscript:\n{transcript_text}"},
            ],
            temperature=0, max_tokens=800,
        )
        raw = json.loads(insight_resp.choices[0].message.content or "{}")
        insights = parse_insights(raw)
        all_insights.append({"meeting": mtg, "insights": insights})
        all_summaries.append({"meeting": mtg, "summary": insights["summary"]})

        print(f"   Action items: {len(insights['action_items'])}  |  Decisions: {len(insights['key_decisions'])}  |  Follow-ups: {len(insights['follow_ups'])}")

    total_chunks = len(all_chunks)
    print(f"\n  Total across all meetings: {total_chunks} chunks ready for retrieval\n")

    # ── Step 2: Print per-meeting insights ───────────────────────────────────
    print("=" * 72)
    print("  INSIGHTS PER MEETING")
    print("=" * 72)

    for item in all_insights:
        mtg = item["meeting"]
        ins = item["insights"]
        print(f"\n  [{mtg['subject']}]  {mtg['date']}")
        print(f"  Summary: {ins['summary'][:200]}...")
        print(f"  Action Items:")
        for ai in ins["action_items"]:
            due = ai.get("due_date") or "no date"
            print(f"    - {ai['owner']}: {ai['task']} (due: {due})")
        print(f"  Key Decisions:")
        for kd in ins["key_decisions"]:
            print(f"    - {kd['decision']}")
        if ins["follow_ups"]:
            print(f"  Follow-ups:")
            for fu in ins["follow_ups"]:
                print(f"    - {fu}")

    # ── Step 3: Cross-meeting Q&A ─────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  CROSS-MEETING Q & A")
    print("=" * 72)

    for qi, question in enumerate(CROSS_MEETING_QUESTIONS, 1):
        print(f"\nQ{qi}: {question}")
        print("-" * 72)

        # Embed query
        q_emb = await embed_single(question)

        # Cosine ranking across ALL chunks from ALL meetings
        cos_scores = sorted(
            [(i, _cosine(q_emb, emb)) for i, emb in enumerate(all_embeddings)],
            key=lambda x: x[1], reverse=True,
        )
        vec_ranks = {idx: r + 1 for r, (idx, _) in enumerate(cos_scores)}
        cos_map   = dict(cos_scores)

        # BM25 ranking across all chunks
        bm25_scores = [
            (i, _bm25_score(all_chunks[i]["chunk"].text, question))
            for i in range(total_chunks)
        ]
        bm25_hits = sorted(
            [(i, s) for i, s in bm25_scores if s > 0],
            key=lambda x: x[1], reverse=True,
        )
        bm25_ranks = {i: r + 1 for r, (i, _) in enumerate(bm25_hits)}

        # RRF fusion
        fused = _rrf_fuse(vec_ranks, bm25_ranks, pool=total_chunks, top_k=6)

        # Build handler result (transcript chunks)
        handler_result = []
        seen_meetings = set()
        for idx, rrf_score in fused:
            item  = all_chunks[idx]
            mtg   = item["meeting"]
            chunk = item["chunk"]
            handler_result.append({
                "source_type":      "transcript",
                "meeting_id":       mtg["id"],
                "meeting_title":    mtg["subject"],
                "meeting_date":     mtg["date"],
                "speaker_name":     chunk.speaker,
                "text":             chunk.text,
                "timestamp_ms":     chunk.start_ms,
                "similarity_score": cos_map[idx],
            })
            seen_meetings.add(mtg["id"])

        # Add insights for meetings that appear in retrieved chunks (HYBRID style)
        for item in all_insights:
            if item["meeting"]["id"] in seen_meetings:
                mtg = item["meeting"]
                ins = item["insights"]
                handler_result.insert(0, {
                    "source_type":   "insights",
                    "meeting_id":    mtg["id"],
                    "meeting_title": mtg["subject"],
                    "meeting_date":  mtg["date"],
                    "summary":       ins["summary"],
                    "action_items":  [f"{a['owner']}: {a['task']}" for a in ins["action_items"]],
                    "key_topics":    [d["decision"] for d in ins["key_decisions"]],
                })

        # Show which meetings were pulled
        print(f"  Meetings retrieved from:")
        for mid in seen_meetings:
            mtg_name = next(m["subject"] for m in MEETINGS if m["id"] == mid)
            print(f"    - {mtg_name}")

        print(f"  Chunks retrieved: {len(fused)}  |  Insights blocks: {len(seen_meetings)}")

        # Top chunks
        print(f"  Top chunks:")
        for rank, (idx, rrf_score) in enumerate(fused[:4], 1):
            item  = all_chunks[idx]
            chunk = item["chunk"]
            mtg   = item["meeting"]
            ts    = f"{chunk.start_ms // 1000 // 60:02d}:{chunk.start_ms // 1000 % 60:02d}"
            print(f"    [{rank}] [{mtg['subject'][:30]}] [{ts}] {chunk.speaker}: {chunk.text[:70]}...")

        # Generate answer
        answer = await generate_answer(question, "HYBRID", handler_result, [])
        print(f"\n  Answer:")
        for line in answer.splitlines():
            print(f"    {line}")
        print()

    print("=" * 72)
    print("  Done.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(run())
