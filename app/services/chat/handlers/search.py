"""SEARCH handler — semantic search over transcript chunks.

Embed query → hybrid retrieval (BM25 + vector + RRF + round-robin) → group
by meeting → format SEARCH context → LLM with SEARCH_SYSTEM.
"""
from __future__ import annotations

from typing import Awaitable, Callable
from uuid import UUID

from app.services.chat.answer import (
    compose_with_llm,
    format_meeting_for_search,
    group_chunks_by_meeting,
)
from app.services.chat.config import MAX_TOKENS_SEARCH, SEARCH_TOP_K, TEMPERATURE_ANSWER
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import (
    ChunkSearcher,
    LLMClient,
    MeetingMeta,
    MetadataRepo,
)
from app.services.chat.prompts import SEARCH_SYSTEM
from app.services.chat.sources import build_sources_from_chunks
from app.services.llm.deployments import llm_for_answer

# Type alias: caller passes a function that embeds a string → vector.
# Production wiring uses `app.services.ingestion.embedder.embed_single`.
EmbedFn = Callable[[str], Awaitable[list[float]]]

_NO_RESULTS = "I couldn't find anything matching that in your meetings."


async def handle_search(
    *,
    query: str,
    search_query: str,                    # router's cleaned, embedding-optimised reformulation
    meeting_ids: list[UUID],
    filters: dict,                        # router's filters (uses speaker_graph_ids)
    metadata_repo: MetadataRepo,
    chunk_searcher: ChunkSearcher,
    llm: LLMClient,
    embed: EmbedFn,
    history: list[dict[str, str]] | None = None,
    top_k: int = SEARCH_TOP_K,
    user_context: str | None = None,
) -> HandlerResult:
    if not meeting_ids:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    query_embedding = await embed(search_query or query)
    chunks = await chunk_searcher.hybrid_search(
        query_embedding=query_embedding,
        query_text=search_query or query,
        meeting_ids=meeting_ids,
        filters=filters,
        top_k=top_k,
    )
    if not chunks:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    # Need meeting metadata for the prompt header (title + date — chunks have
    # title but not always the canonical date). Fetch in one round-trip.
    referenced = list({c.meeting_id for c in chunks})
    meetings = await metadata_repo.get_meetings(referenced)
    meta_by_id: dict[UUID, MeetingMeta] = {m.meeting_id: m for m in meetings}

    grouped = group_chunks_by_meeting(chunks)
    blocks: list[str] = []
    for mid, mid_chunks in grouped.items():
        meta = meta_by_id.get(mid)
        if meta is None:
            # Build a minimal MeetingMeta from chunk fields if metadata missed.
            from app.services.chat.interfaces import MeetingMeta as _MM
            meta = _MM(
                meeting_id=mid,
                title=mid_chunks[0].meeting_title or "(untitled)",
                date=mid_chunks[0].meeting_date,  # type: ignore[arg-type]
                duration_minutes=None,
                organizer_name=None,
                participants=[],
                status="ready",
            )
        blocks.append(format_meeting_for_search(meta, mid_chunks))

    context = "\n\n".join(blocks)
    answer = await compose_with_llm(
        llm=llm,
        deployment=llm_for_answer(),
        system_prompt=SEARCH_SYSTEM,
        context_block=context,
        user_query=query,
        history=history,
        no_results_msg=_NO_RESULTS,
        max_tokens=MAX_TOKENS_SEARCH,
        temperature=TEMPERATURE_ANSWER,
        user_context=user_context,
    )
    return HandlerResult(
        answer=answer,
        sources=build_sources_from_chunks(chunks),
        referenced_meeting_ids=referenced,
    )
