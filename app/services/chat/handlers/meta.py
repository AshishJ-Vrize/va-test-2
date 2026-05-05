"""META handler — answers about meetings as events.

Pulls meetings + participants for the effective scope, formats a context
block, and lets the LLM compose a natural answer (list, attendance, dates).

Speaker / date filters from the router have already been applied at the
orchestrator's scope-resolution step, so by the time we get here, meeting_ids
is the final set we should describe.
"""
from __future__ import annotations

from uuid import UUID

from app.services.chat.answer import (
    compose_with_llm,
    format_meeting_for_meta,
)
from app.services.chat.config import MAX_TOKENS_META, TEMPERATURE_ANSWER
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import LLMClient, MetadataRepo
from app.services.chat.prompts import META_SYSTEM
from app.services.chat.sources import build_sources_from_meetings
from app.services.llm.deployments import llm_for_answer

_NO_RESULTS = "I couldn't find anything matching that in your meetings."


async def handle_meta(
    *,
    query: str,
    meeting_ids: list[UUID],
    metadata_repo: MetadataRepo,
    llm: LLMClient,
    history: list[dict[str, str]] | None = None,
    user_context: str | None = None,
) -> HandlerResult:
    if not meeting_ids:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    meetings = await metadata_repo.get_meetings(meeting_ids)
    if not meetings:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    blocks = [format_meeting_for_meta(m) for m in meetings]
    context = "\n\n".join(blocks)

    answer = await compose_with_llm(
        llm=llm,
        deployment=llm_for_answer(),
        system_prompt=META_SYSTEM,
        context_block=context,
        user_query=query,
        history=history,
        no_results_msg=_NO_RESULTS,
        max_tokens=MAX_TOKENS_META,
        temperature=TEMPERATURE_ANSWER,
        user_context=user_context,
    )
    return HandlerResult(
        answer=answer,
        sources=build_sources_from_meetings(meetings),
        referenced_meeting_ids=[m.meeting_id for m in meetings],
    )
