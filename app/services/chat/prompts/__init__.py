"""Prompt registry — exports ROUTE_PROMPTS dict for handlers to look up by route.

Patterns sourced from ContextIQ-main (rag_service.py) noted in module
docstrings: INDEXED-MEETINGS calendar header, numbered SPEAKER ACCURACY RULES
(CRITICAL) A-D, "If not found say exactly" failure templates, anti-hallucination
guards, no-inline-citations rule (sources are surfaced in the UI panel).
"""
from __future__ import annotations

from app.services.chat.prompts.clarify import CLARIFY_SYSTEM, CLARIFY_TEMPLATE_FALLBACK
from app.services.chat.prompts.compare import COMPARE_SYSTEM
from app.services.chat.prompts.general import GENERAL_GK_SYSTEM, GENERAL_REFUSE_TEMPLATE
from app.services.chat.prompts.hybrid import HYBRID_SYSTEM
from app.services.chat.prompts.meta import META_SYSTEM
from app.services.chat.prompts.router import ROUTER_SYSTEM
from app.services.chat.prompts.search import SEARCH_SYSTEM
from app.services.chat.prompts.structured import STRUCTURED_SYSTEM


ROUTE_PROMPTS: dict[str, str] = {
    "META": META_SYSTEM,
    "STRUCTURED_LLM": STRUCTURED_SYSTEM,
    "SEARCH": SEARCH_SYSTEM,
    "HYBRID": HYBRID_SYSTEM,
    "COMPARE": COMPARE_SYSTEM,
    "GENERAL_GK": GENERAL_GK_SYSTEM,
    "CLARIFY": CLARIFY_SYSTEM,
    # STRUCTURED_DIRECT and GENERAL_REFUSE don't go through the LLM — no prompt.
}

__all__ = [
    "ROUTE_PROMPTS",
    "ROUTER_SYSTEM",
    "META_SYSTEM",
    "STRUCTURED_SYSTEM",
    "SEARCH_SYSTEM",
    "HYBRID_SYSTEM",
    "COMPARE_SYSTEM",
    "GENERAL_GK_SYSTEM",
    "GENERAL_REFUSE_TEMPLATE",
    "CLARIFY_SYSTEM",
    "CLARIFY_TEMPLATE_FALLBACK",
]
