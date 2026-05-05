"""Router — LLM-based query classifier.

Two stages:
  1. LLM classifies the query into a route + extracts filters + signals scope intent.
     (Uses gpt-4.1-mini per `llm_for_router()`; today's date injected so relative
     date phrases like "last week" can be resolved.)
  2. If a speaker_name was extracted, resolve it to graph_ids via the tenant-wide
     SpeakerResolver. The resolution result is attached to the filters so handlers
     don't have to re-resolve.

On any failure (LLM error, invalid JSON, validation), fall back to a SEARCH route
with the raw query — degrades gracefully rather than 500'ing the request.

Tests inject fake LLMClient + fake SpeakerResolver; production wires real ones.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from app.services.chat.config import (
    MAX_TOKENS_ROUTER,
    RBAC_WITHIN_DAYS,
    TEMPERATURE_ROUTER,
)
from app.services.chat.interfaces import (
    LLMClient,
    RouterDecision,
    SpeakerResolver,
)
from app.services.chat.prompts import ROUTER_SYSTEM
from app.services.llm.deployments import llm_for_router

log = logging.getLogger(__name__)


_VALID_ROUTES = {
    "META",
    "STRUCTURED_DIRECT",
    "STRUCTURED_LLM",
    "SEARCH",
    "HYBRID",
    "COMPARE",
    "GENERAL_GK",
    "CLARIFY",
    "GENERAL_REFUSE",
}


_FILTER_KEYS = (
    "speaker_name",
    "date_from",
    "date_to",
    "meeting_titles",
    "keyword_focus",
    "structured_intent",   # only set when route is STRUCTURED_DIRECT
)


_VALID_STRUCTURED_INTENTS = {
    "digest", "list_actions", "list_decisions", "list_followups",
}


async def classify_query(
    query: str,
    *,
    llm: LLMClient,
    speaker_resolver: SpeakerResolver,
    today: date | None = None,
) -> RouterDecision:
    """Classify the query and return a fully-populated RouterDecision.

    Side effects: none on the DB beyond the speaker resolver's reads.
    """
    today_str = (today or date.today()).isoformat()
    system_prompt = (
        ROUTER_SYSTEM
        .replace("{TODAY}", today_str)
        .replace("{WITHIN_DAYS}", str(RBAC_WITHIN_DAYS))
    )

    raw: dict = {}
    try:
        raw = await llm.complete_json(
            deployment=llm_for_router(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=MAX_TOKENS_ROUTER,
            temperature=TEMPERATURE_ROUTER,
        )
    except Exception as exc:
        log.error("router: LLM call failed (%s) — falling back to SEARCH", exc)

    decision = _parse(raw, fallback_query=query)

    # Override the LLM's out_of_window flag with deterministic math against
    # the configured RBAC_WITHIN_DAYS. The LLM gets the arithmetic wrong
    # sometimes; we trust the dates it extracted, not its date-math.
    decision.out_of_window = _is_out_of_window(
        decision.filters.get("date_from"),
        decision.filters.get("date_to"),
        today=today or date.today(),
        within_days=RBAC_WITHIN_DAYS,
    )

    # Resolve speaker name → graph_ids (for SEARCH speaker filtering and for
    # the disambiguation flow).
    speaker_name = decision.filters.get("speaker_name")
    if speaker_name:
        try:
            candidates = await speaker_resolver.resolve(speaker_name)
        except Exception as exc:
            log.warning("router: speaker resolution failed for %r: %s", speaker_name, exc)
            candidates = []

        decision.filters["speaker_graph_ids"] = (
            [c.graph_id for c in candidates] if candidates else None
        )
        decision.filters["speaker_disambiguation_needed"] = len(candidates) > 1
        decision.filters["speaker_candidates"] = (
            [
                {"name": c.name, "email": c.email, "graph_id": c.graph_id}
                for c in candidates
            ]
            if len(candidates) > 1
            else None
        )
    else:
        decision.filters.setdefault("speaker_graph_ids", None)
        decision.filters.setdefault("speaker_disambiguation_needed", False)
        decision.filters.setdefault("speaker_candidates", None)

    log.info(
        "router: query=%r route=%s speaker=%r resolved_gids=%d "
        "date_from=%s date_to=%s titles=%s out_of_window=%s",
        query[:80], decision.route, speaker_name,
        len(decision.filters.get("speaker_graph_ids") or []),
        decision.filters.get("date_from"), decision.filters.get("date_to"),
        decision.filters.get("meeting_titles"), decision.out_of_window,
    )
    return decision


# ── Parsing & validation ──────────────────────────────────────────────────────

def _parse(raw: dict, fallback_query: str) -> RouterDecision:
    """Parse the LLM's JSON output into RouterDecision; fall back on garbage."""
    if not isinstance(raw, dict) or not raw:
        return _fallback(fallback_query)

    route = (raw.get("route") or "").upper()
    if route not in _VALID_ROUTES:
        log.warning("router: invalid route %r — falling back to SEARCH", route)
        route = "SEARCH"

    raw_filters = raw.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raw_filters = {}
    filters: dict[str, Any] = {key: raw_filters.get(key) for key in _FILTER_KEYS}

    # Validate structured_intent — drop unknown values rather than letting them
    # leak into the handler.
    si = filters.get("structured_intent")
    if si is not None and si not in _VALID_STRUCTURED_INTENTS:
        log.warning("router: invalid structured_intent %r — clearing", si)
        filters["structured_intent"] = None

    raw_scope = raw.get("scope_intent") or {}
    if not isinstance(raw_scope, dict):
        raw_scope = {}
    scope_intent = {
        "needs_change": bool(raw_scope.get("needs_change", False)),
        "reason": str(raw_scope.get("reason") or ""),
    }

    out_of_win = bool(raw.get("out_of_window", False))
    search_query = (raw.get("search_query") or fallback_query).strip() or fallback_query

    return RouterDecision(
        route=route,
        filters=filters,
        scope_intent=scope_intent,
        out_of_window=out_of_win,
        search_query=search_query,
    )


def _fallback(query: str) -> RouterDecision:
    """Default to SEARCH with the raw query when the router fails."""
    return RouterDecision(
        route="SEARCH",
        filters={key: None for key in _FILTER_KEYS},
        scope_intent={"needs_change": False, "reason": ""},
        out_of_window=False,
        search_query=query,
    )


def _is_out_of_window(
    date_from: str | None,
    date_to: str | None,
    *,
    today: date,
    within_days: int,
) -> bool:
    """Deterministic check: does any extracted date fall outside (today − within_days)?

    Returns False when no dates were extracted (nothing to flag).
    Bad ISO strings are silently ignored (don't trip the flag).
    """
    cutoff = today - timedelta(days=within_days)
    for raw in (date_from, date_to):
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            return True
    return False


# Backwards-compatible alias — older tests imported `_is_out_of_30_days`.
def _is_out_of_30_days(date_from, date_to, *, today):
    return _is_out_of_window(date_from, date_to, today=today, within_days=RBAC_WITHIN_DAYS)
