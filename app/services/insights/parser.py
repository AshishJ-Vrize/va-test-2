"""Insight response parser — validate and normalise LLM JSON into insight fields."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_REQUIRED_KEYS = {"summary", "action_items", "key_decisions", "follow_ups"}


def parse_insights(raw: dict) -> dict:
    """
    Validate and normalise the LLM's JSON response.
    Returns a clean dict with all four required keys guaranteed present.
    Raises ValueError on unrecoverable schema errors.
    """
    missing = _REQUIRED_KEYS - raw.keys()
    if missing:
        raise ValueError(f"LLM response missing keys: {missing}")

    return {
        "summary": str(raw["summary"]).strip() if raw["summary"] else "",
        "action_items": _ensure_list(raw["action_items"], "action_items"),
        "key_decisions": _ensure_list(raw["key_decisions"], "key_decisions"),
        "follow_ups": _ensure_list(raw["follow_ups"], "follow_ups"),
    }


def _ensure_list(value, field_name: str) -> list:
    if isinstance(value, list):
        return value
    log.warning("insights parser: '%s' is not a list (%s), defaulting to []", field_name, type(value))
    return []
