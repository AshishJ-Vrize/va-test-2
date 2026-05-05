"""Unit tests for InsightsRepoImpl pure helpers.

DB-touching methods (get_insights, get_summary_text) are integration-tested
elsewhere — these tests cover the JSONB envelope unwrapping that fixes the
"`{'items': [...]}` literal in LLM context" bug.
"""
from __future__ import annotations

from app.services.chat.repos.insights_repo import _normalize_field


# ── Common shapes from meeting_insights.fields ───────────────────────────────

def test_unwraps_items_envelope():
    assert _normalize_field({"items": ["a", "b", "c"]}) == ["a", "b", "c"]


def test_unwraps_text_envelope():
    assert _normalize_field({"text": "Concise summary."}) == "Concise summary."


def test_handles_none():
    assert _normalize_field(None) is None


def test_handles_empty_dict():
    # Empty dict in JSONB → treat as None so caller can omit the field.
    assert _normalize_field({}) is None


# ── Already-normalized values pass through ───────────────────────────────────

def test_already_a_list():
    assert _normalize_field(["x", "y"]) == ["x", "y"]


def test_already_a_string():
    assert _normalize_field("plain summary") == "plain summary"


# ── Defensive edge cases ──────────────────────────────────────────────────────

def test_items_with_non_list_inner():
    # Defensive — if 'items' was misstored as a single value, wrap it.
    assert _normalize_field({"items": "single"}) == ["single"]


def test_text_with_non_string_inner():
    # Defensive — coerce to str for downstream prompt consumption.
    assert _normalize_field({"text": 42}) == "42"


def test_unrecognized_dict_envelope():
    # Unknown shape — surface stringified so devs see it during testing.
    out = _normalize_field({"unexpected": "shape"})
    assert isinstance(out, str)
    assert "unexpected" in out
