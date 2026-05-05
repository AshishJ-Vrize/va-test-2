"""GENERAL handler — refusal (no LLM) or GK-augmented (LLM only, no DB).

Two sub-modes determined by the router:
  GENERAL_REFUSE  — completely off-topic ("capital of France"). Returns a
                    fixed soft-refusal template. No LLM call. Fast.
  GENERAL_GK      — meeting-management or work-process question that benefits
                    from general knowledge but doesn't need user data
                    ("how can we improve our standups"). LLM answers from
                    own knowledge bound by the GK system prompt.
"""
from __future__ import annotations

from app.services.chat.config import MAX_TOKENS_GENERAL_GK, TEMPERATURE_GENERAL_GK
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import LLMClient
from app.services.chat.prompts import GENERAL_GK_SYSTEM, GENERAL_REFUSE_TEMPLATE
from app.services.llm.deployments import llm_for_answer


def handle_general_refuse() -> HandlerResult:
    """Synchronous — no I/O. Returns the fixed refusal template."""
    return HandlerResult(answer=GENERAL_REFUSE_TEMPLATE)


async def handle_general_gk(
    *,
    query: str,
    llm: LLMClient,
    history: list[dict[str, str]] | None = None,
) -> HandlerResult:
    """LLM-only — no retrieval, no DB.

    Falls back to the refusal template on any LLM failure (we'd rather refuse
    cleanly than emit an error message).
    """
    messages: list[dict] = [{"role": "system", "content": GENERAL_GK_SYSTEM}]
    if history:
        # Cap history; reuses the same window logic as compose_with_llm callers.
        from app.services.chat.answer import _truncate_history
        messages.extend(_truncate_history(history))
    messages.append({"role": "user", "content": query})

    try:
        answer = await llm.complete_text(
            deployment=llm_for_answer(),
            messages=messages,
            max_tokens=MAX_TOKENS_GENERAL_GK,
            temperature=TEMPERATURE_GENERAL_GK,
        )
    except Exception:
        return HandlerResult(answer=GENERAL_REFUSE_TEMPLATE)

    return HandlerResult(answer=answer.strip() or GENERAL_REFUSE_TEMPLATE)
