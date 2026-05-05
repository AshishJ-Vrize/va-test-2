"""COMPARE handler — cross-meeting comparison.

Tiered behaviour by selection size — see app.services.chat.config:

   ≤ COMPARE_MAX_FULL (default 5)
       Full insight bundle per meeting (Summary + Decisions + Actions + Follow-ups).
       The LLM has rich context for fine-grained comparison.

   COMPARE_MAX_FULL+1 .. COMPARE_MAX_SUMMARY (default 6-15)
       Summary text only per meeting. Decisions / actions / follow-ups are
       skipped to keep the LLM context manageable. Comparison stays at the
       theme / topic level rather than item-by-item.

   > COMPARE_MAX_SUMMARY (default 16+)
       Refuse with a helpful message — comparing that many meetings produces
       low-quality results regardless of how much context you stuff in. The
       message tells the user how to either narrow or rephrase.

For ALL paths comparison happens at SUMMARY level (not transcript chunks) —
chunks are too noisy for cross-meeting comparison.
"""
from __future__ import annotations

import string
from uuid import UUID

from app.services.chat.answer import (
    compose_with_llm,
    format_meeting_for_compare,
)
from app.services.chat.config import (
    COMPARE_MAX_FULL,
    COMPARE_MAX_SUMMARY,
    MAX_TOKENS_COMPARE,
    TEMPERATURE_ANSWER,
)
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import (
    InsightsBundle,
    InsightsRepo,
    LLMClient,
    MeetingMeta,
    MetadataRepo,
)
from app.services.chat.prompts import COMPARE_SYSTEM
from app.services.chat.sources import build_sources_from_meetings
from app.services.llm.deployments import llm_for_answer

_NO_RESULTS = (
    "I need at least two meetings to compare. Select more meetings or name "
    "specific meetings in your question."
)
_NO_OVERLAP = (
    "These meetings cover different topics — there's no meaningful overlap to "
    "compare."
)


def _too_many_message(n: int) -> str:
    return (
        f"You've selected {n} meetings to compare — that's more than I can "
        f"compare meaningfully (limit: {COMPARE_MAX_SUMMARY}). Try one of:\n\n"
        f"- Narrow your selection to {COMPARE_MAX_FULL} or fewer meetings for "
        f"the most detailed comparison\n"
        f"- Ask \"what are the common themes across these meetings\" — I can "
        f"give you a higher-level synthesis instead\n"
        f"- Ask a more specific question like \"did we discuss <topic> in "
        f"these meetings\" — I can search across all of them."
    )


async def handle_compare(
    *,
    query: str,
    meeting_ids: list[UUID],
    metadata_repo: MetadataRepo,
    insights_repo: InsightsRepo,
    llm: LLMClient,
    history: list[dict[str, str]] | None = None,
    user_context: str | None = None,
) -> HandlerResult:
    n = len(meeting_ids)
    if n < 2:
        return HandlerResult(answer=_NO_RESULTS)
    if n > COMPARE_MAX_SUMMARY:
        return HandlerResult(answer=_too_many_message(n))

    summary_only = n > COMPARE_MAX_FULL

    meetings = await metadata_repo.get_meetings(meeting_ids)
    if len(meetings) < 2:
        return HandlerResult(answer=_NO_RESULTS)

    # Sort meetings chronologically (oldest first) — labels A, B, C track time.
    meetings.sort(key=lambda m: (m.date or 0, str(m.meeting_id)))

    # In summary-only mode, skip the bulk insight fetch entirely.
    if summary_only:
        insights_by_id: dict[UUID, InsightsBundle] = {}
    else:
        insights = await insights_repo.get_insights(meeting_ids)
        insights_by_id = {ib.meeting_id: ib for ib in insights}

    blocks: list[str] = []
    referenced: list[UUID] = []
    for label, m in zip(string.ascii_uppercase, meetings):
        # Always try the cached MOM summary first; fall back to insight summary.
        summary_text = await insights_repo.get_summary_text(m.meeting_id)
        ib = insights_by_id.get(m.meeting_id)   # None in summary-only mode
        block = format_meeting_for_compare(m, summary_text, ib, label=label)
        blocks.append(block)
        referenced.append(m.meeting_id)

    context = "\n\n".join(blocks)
    answer = await compose_with_llm(
        llm=llm,
        deployment=llm_for_answer(),
        system_prompt=COMPARE_SYSTEM,
        context_block=context,
        user_query=query,
        history=history,
        no_results_msg=_NO_OVERLAP,
        max_tokens=MAX_TOKENS_COMPARE,
        temperature=TEMPERATURE_ANSWER,
        user_context=user_context,
    )
    return HandlerResult(
        answer=answer,
        sources=build_sources_from_meetings(meetings),
        referenced_meeting_ids=referenced,
    )
