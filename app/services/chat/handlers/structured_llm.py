"""STRUCTURED_LLM handler — narrative synthesis using all insight fields.

Used for "summarise / tl;dr / gist" queries. Pulls the full insight bundle
for each meeting and lets the LLM compose a coherent prose answer.
"""
from __future__ import annotations

from uuid import UUID

from app.services.chat.answer import (
    compose_with_llm,
    format_meeting_for_structured,
)
from app.services.chat.config import MAX_TOKENS_STRUCTURED_LLM, TEMPERATURE_ANSWER
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import (
    InsightsRepo,
    LLMClient,
    MeetingMeta,
    MetadataRepo,
)
from app.services.chat.prompts import STRUCTURED_SYSTEM
from app.services.chat.sources import build_sources_from_insights
from app.services.llm.deployments import llm_for_answer

_NO_RESULTS = "I couldn't find anything matching that in your meetings."


async def handle_structured_llm(
    *,
    query: str,
    meeting_ids: list[UUID],
    metadata_repo: MetadataRepo,
    insights_repo: InsightsRepo,
    llm: LLMClient,
    history: list[dict[str, str]] | None = None,
    user_context: str | None = None,
) -> HandlerResult:
    if not meeting_ids:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    # Fetch insights + meeting meta in parallel-ish (sequential for now to
    # keep handler simple; orchestrator can parallelise in a later pass).
    insights = await insights_repo.get_insights(meeting_ids)
    if not insights:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    meetings = await metadata_repo.get_meetings(meeting_ids)
    meta_by_id: dict[UUID, MeetingMeta] = {m.meeting_id: m for m in meetings}

    # Build context blocks — one per meeting that has insights.
    blocks: list[str] = []
    referenced_ids: list[UUID] = []
    for ib in insights:
        meta = meta_by_id.get(ib.meeting_id)
        if meta is None:
            # Edge: insights row exists but no meeting? Skip — orphaned data.
            continue
        blocks.append(format_meeting_for_structured(meta, ib))
        referenced_ids.append(ib.meeting_id)

    context = "\n\n".join(blocks)
    answer = await compose_with_llm(
        llm=llm,
        deployment=llm_for_answer(),
        system_prompt=STRUCTURED_SYSTEM,
        context_block=context,
        user_query=query,
        history=history,
        no_results_msg=_NO_RESULTS,
        max_tokens=MAX_TOKENS_STRUCTURED_LLM,
        temperature=TEMPERATURE_ANSWER,
        user_context=user_context,
    )
    return HandlerResult(
        answer=answer,
        sources=build_sources_from_insights(insights),
        referenced_meeting_ids=referenced_ids,
    )
