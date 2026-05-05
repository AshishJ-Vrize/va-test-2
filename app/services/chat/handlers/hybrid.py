"""HYBRID handler — synthesises insights AND quotes from transcripts.

Runs the structured-insight fetch and the chunk search in parallel, builds
a combined context block (insights first, then chunks per meeting), and lets
the LLM produce a single answer with HYBRID_SYSTEM.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from app.services.chat.answer import (
    compose_with_llm,
    format_meeting_for_search,
    format_meeting_for_structured,
    group_chunks_by_meeting,
)
from app.services.chat.config import MAX_TOKENS_HYBRID, SEARCH_TOP_K, TEMPERATURE_ANSWER
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.handlers.search import EmbedFn
from app.services.chat.interfaces import (
    ChunkSearcher,
    InsightsBundle,
    InsightsRepo,
    LLMClient,
    MeetingMeta,
    MetadataRepo,
)
from app.services.chat.prompts import HYBRID_SYSTEM
from app.services.chat.sources import (
    build_sources_from_chunks,
    build_sources_from_insights,
    merge_sources,
)
from app.services.llm.deployments import llm_for_answer

_NO_RESULTS = "I couldn't find anything relevant in your meetings."


async def handle_hybrid(
    *,
    query: str,
    search_query: str,
    meeting_ids: list[UUID],
    filters: dict,
    metadata_repo: MetadataRepo,
    insights_repo: InsightsRepo,
    chunk_searcher: ChunkSearcher,
    llm: LLMClient,
    embed: EmbedFn,
    history: list[dict[str, str]] | None = None,
    top_k: int = SEARCH_TOP_K,
    user_context: str | None = None,
) -> HandlerResult:
    if not meeting_ids:
        return HandlerResult(answer=_NO_RESULTS)

    # Embed first because asyncio.gather + Azure rate limits don't like
    # bunching N calls on the same deployment unnecessarily.
    query_embedding = await embed(search_query or query)

    # Fetch in parallel — independent reads against different tables.
    insights, chunks, meetings = await asyncio.gather(
        insights_repo.get_insights(meeting_ids),
        chunk_searcher.hybrid_search(
            query_embedding=query_embedding,
            query_text=search_query or query,
            meeting_ids=meeting_ids,
            filters=filters,
            top_k=top_k,
        ),
        metadata_repo.get_meetings(meeting_ids),
    )

    meta_by_id: dict[UUID, MeetingMeta] = {m.meeting_id: m for m in meetings}
    insights_by_meeting: dict[UUID, InsightsBundle] = {ib.meeting_id: ib for ib in insights}

    if not chunks and not insights:
        return HandlerResult(answer=_NO_RESULTS)

    # Build context: for each meeting referenced (by either chunks or insights),
    # render insight-block and/or chunk-block.
    referenced_ids: list[UUID] = []
    seen: set[UUID] = set()
    blocks: list[str] = []

    grouped_chunks = group_chunks_by_meeting(chunks)

    # Order: meetings appearing in chunks first (most-relevant by RRF), then
    # any insight-only meetings.
    for mid in list(grouped_chunks.keys()) + [
        m for m in insights_by_meeting if m not in grouped_chunks
    ]:
        if mid in seen:
            continue
        seen.add(mid)
        meta = meta_by_id.get(mid)
        if meta is None:
            continue
        referenced_ids.append(mid)

        ib = insights_by_meeting.get(mid)
        if ib is not None:
            blocks.append(format_meeting_for_structured(meta, ib))
        if mid in grouped_chunks:
            blocks.append(format_meeting_for_search(meta, grouped_chunks[mid]))

    context = "\n\n".join(blocks)
    answer = await compose_with_llm(
        llm=llm,
        deployment=llm_for_answer(),
        system_prompt=HYBRID_SYSTEM,
        context_block=context,
        user_query=query,
        history=history,
        no_results_msg=_NO_RESULTS,
        max_tokens=MAX_TOKENS_HYBRID,
        temperature=TEMPERATURE_ANSWER,
        user_context=user_context,
    )
    return HandlerResult(
        answer=answer,
        # Chunks first so transcript cards rank higher than insight-only cards.
        sources=merge_sources(
            build_sources_from_chunks(chunks),
            build_sources_from_insights(insights),
        ),
        referenced_meeting_ids=referenced_ids,
    )
