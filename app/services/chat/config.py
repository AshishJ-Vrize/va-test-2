"""Tunable knobs for the chat layer — read once, easy to override.

Every constant here is overridable via an env var of the same name. Set in
`.env` to A/B-test without code changes:

    CHAT_HISTORY_TURN_PAIRS=8
    CHAT_COMPARE_MAX_FULL=3
    CHAT_MAX_TOKENS_HYBRID=1200

Code shouldn't read environment variables directly — always import the
constant from here so the override path is uniform and testable.

Categories:
    Conversation history    HISTORY_*, CLARIFY_HISTORY_MSGS, SESSION_TURN_WINDOW
    COMPARE thresholds      COMPARE_MAX_*
    Retrieval               SEARCH_TOP_K, RETRIEVAL_*
    LLM token budgets       MAX_TOKENS_<task>
    LLM temperatures        TEMPERATURE_<task>
    UI / presentation       MAX_SOURCE_CARDS
    RBAC                    RBAC_WITHIN_DAYS
"""
from __future__ import annotations

import os


def _int(env_name: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── Conversation history ──────────────────────────────────────────────────────

# How many user+assistant PAIRS are passed to the answer LLM (so 2 × this many messages).
HISTORY_TURN_PAIRS: int = _int("CHAT_HISTORY_TURN_PAIRS", 5)

# Cap on total characters across history messages.
HISTORY_MAX_CHARS: int = _int("CHAT_HISTORY_MAX_CHARS", 6000)

# Trailing message count CLARIFY sees.
CLARIFY_HISTORY_MSGS: int = _int("CHAT_CLARIFY_HISTORY_MSGS", 4)

# Total turns kept in the SessionStore rolling window.
SESSION_TURN_WINDOW: int = _int("CHAT_SESSION_TURN_WINDOW", 20)


# ── COMPARE thresholds ────────────────────────────────────────────────────────

COMPARE_MAX_FULL: int = _int("CHAT_COMPARE_MAX_FULL", 5)
COMPARE_MAX_SUMMARY: int = _int("CHAT_COMPARE_MAX_SUMMARY", 15)


# ── Retrieval ─────────────────────────────────────────────────────────────────

# Top-K chunks returned after RRF + round-robin. Used by SEARCH and HYBRID.
SEARCH_TOP_K: int = _int("CHAT_SEARCH_TOP_K", 10)

# RRF fusion constant — higher values smooth rank differences. 60 is the
# standard from the RRF paper; rarely needs tuning.
RETRIEVAL_RRF_K: int = _int("CHAT_RETRIEVAL_RRF_K", 60)

# Candidate pool size = top_k × this multiplier per retrieval signal.
# Higher = better recall, slightly more DB work.
RETRIEVAL_POOL_MULTIPLIER: int = _int("CHAT_RETRIEVAL_POOL_MULTIPLIER", 2)


# ── LLM token budgets per task ────────────────────────────────────────────────

# Routing classifier — small JSON output, doesn't need much.
MAX_TOKENS_ROUTER: int = _int("CHAT_MAX_TOKENS_ROUTER", 400)

# Per-handler answer budgets. Compare and structured/hybrid summaries can
# legitimately be longer than search/meta answers.
MAX_TOKENS_META: int = _int("CHAT_MAX_TOKENS_META", 600)
MAX_TOKENS_SEARCH: int = _int("CHAT_MAX_TOKENS_SEARCH", 600)
MAX_TOKENS_STRUCTURED_LLM: int = _int("CHAT_MAX_TOKENS_STRUCTURED_LLM", 900)
MAX_TOKENS_HYBRID: int = _int("CHAT_MAX_TOKENS_HYBRID", 900)
MAX_TOKENS_COMPARE: int = _int("CHAT_MAX_TOKENS_COMPARE", 1000)
MAX_TOKENS_GENERAL_GK: int = _int("CHAT_MAX_TOKENS_GENERAL_GK", 500)
MAX_TOKENS_CLARIFY: int = _int("CHAT_MAX_TOKENS_CLARIFY", 200)


# ── LLM temperatures per task ─────────────────────────────────────────────────

# Router stays deterministic — never want creative classification.
TEMPERATURE_ROUTER: float = _float("CHAT_TEMPERATURE_ROUTER", 0.0)

# Answer composition — moderate sampling for natural prose without drift.
TEMPERATURE_ANSWER: float = _float("CHAT_TEMPERATURE_ANSWER", 0.3)

# General-knowledge questions — slightly more creative.
TEMPERATURE_GENERAL_GK: float = _float("CHAT_TEMPERATURE_GENERAL_GK", 0.4)

# Clarification questions — same as answer.
TEMPERATURE_CLARIFY: float = _float("CHAT_TEMPERATURE_CLARIFY", 0.3)


# ── UI / presentation ─────────────────────────────────────────────────────────

# Max source cards rendered below the answer.
MAX_SOURCE_CARDS: int = _int("CHAT_MAX_SOURCE_CARDS", 5)


# ── RBAC ──────────────────────────────────────────────────────────────────────

# Recency window for the RBAC scope. Search is bounded to meetings within
# this many days. Also drives the router's out-of-window flag.
# Set to 0 to disable the date window (only the count cap applies).
RBAC_WITHIN_DAYS: int = _int("CHAT_RBAC_WITHIN_DAYS", 30)

# Hard cap on how many of the user's most-recent meetings they can search.
# Set to 0 to disable the count cap (only the date window applies).
#
# RBAC_WITHIN_DAYS and RBAC_MAX_MEETINGS combine independently — three modes:
#   only days   → RBAC_WITHIN_DAYS > 0,  RBAC_MAX_MEETINGS = 0
#   only count  → RBAC_WITHIN_DAYS = 0,  RBAC_MAX_MEETINGS > 0
#   both (∩)    → RBAC_WITHIN_DAYS > 0,  RBAC_MAX_MEETINGS > 0  (whichever
#                 is more restrictive wins)
# Both = 0 is allowed but means "no recency RBAC", relying purely on the
# meeting_participants membership check — only do this if you know what you want.
RBAC_MAX_MEETINGS: int = _int("CHAT_RBAC_MAX_MEETINGS", 30)
