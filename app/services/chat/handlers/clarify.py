"""CLARIFY handler — generate a friendly disambiguating question.

Used when the LLM router can't confidently classify the query (typically a
short fragment like "yes", "do it", "include them", or vague phrasing like
"the meeting"). Better UX than refusing.

LLM-only (no DB). Falls back to a generic template on LLM failure.
"""
from __future__ import annotations

import logging

from app.services.chat.config import (
    CLARIFY_HISTORY_MSGS,
    MAX_TOKENS_CLARIFY,
    TEMPERATURE_CLARIFY,
)
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import LLMClient
from app.services.chat.prompts import CLARIFY_SYSTEM, CLARIFY_TEMPLATE_FALLBACK
from app.services.llm.deployments import llm_for_router

log = logging.getLogger(__name__)


async def handle_clarify(
    *,
    query: str,
    llm: LLMClient,
    history: list[dict[str, str]] | None = None,
) -> HandlerResult:
    """Ask the LLM to generate a 1-2 sentence clarifying question.

    Uses the router model (gpt-4.1-mini) since this is a short prompt and
    latency matters more than depth. Includes recent history so the LLM can
    recognise patterns like 'yes' replies to a previous bot suggestion.
    """
    messages: list[dict] = [{"role": "system", "content": CLARIFY_SYSTEM}]
    if history:
        # Keep last CLARIFY_HISTORY_MSGS messages — enough to see the prior
        # bot suggestion when the user replies "yes" / "do it" / etc.
        messages.extend(history[-CLARIFY_HISTORY_MSGS:])
    messages.append({"role": "user", "content": query})

    try:
        answer = await llm.complete_text(
            deployment=llm_for_router(),
            messages=messages,
            max_tokens=MAX_TOKENS_CLARIFY,
            temperature=TEMPERATURE_CLARIFY,
        )
    except Exception as exc:
        log.warning("clarify: LLM call failed (%s) — using template", exc)
        return HandlerResult(answer=CLARIFY_TEMPLATE_FALLBACK)

    return HandlerResult(answer=answer.strip() or CLARIFY_TEMPLATE_FALLBACK)
