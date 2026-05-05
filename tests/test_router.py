"""Unit tests for the router classifier.

Uses fake LLMClient + fake SpeakerResolver — no Azure / DB calls.
Validates the contract (route validation, filter parsing, fallback behavior,
speaker resolution wiring) more than the LLM's quality (that's a manual /
eval-harness concern).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.services.chat.interfaces import SpeakerCandidate
from app.services.chat.router import (
    _VALID_ROUTES,
    _fallback,
    _parse,
    classify_query,
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeLLM:
    """Returns canned JSON for complete_json calls."""

    def __init__(self, response: dict | None = None, raise_on_call: Exception | None = None):
        self._response = response or {}
        self._raise = raise_on_call
        self.last_messages: list[dict] = []
        self.last_deployment: str | None = None

    async def complete_text(self, deployment, messages, max_tokens=600, temperature=0.3):
        return ""

    async def complete_json(self, deployment, messages, max_tokens=400, temperature=0.0):
        self.last_messages = messages
        self.last_deployment = deployment
        if self._raise:
            raise self._raise
        return self._response


class FakeResolver:
    def __init__(self, candidates: list[SpeakerCandidate] | None = None):
        self._candidates = candidates or []
        self.last_name: str | None = None

    async def resolve(self, name: str) -> list[SpeakerCandidate]:
        self.last_name = name
        return self._candidates


# ── _parse — pure validation tests (no LLM, no DB) ────────────────────────────

class TestParse:
    def test_well_formed_input_passes_through(self):
        raw = {
            "route": "SEARCH",
            "filters": {
                "speaker_name": "Sarah",
                "date_from": "2026-04-01",
                "date_to": "2026-04-30",
                "meeting_titles": None,
                "keyword_focus": "pricing",
            },
            "scope_intent": {"needs_change": False, "reason": ""},
            "out_of_window": False,
            "search_query": "What did Sarah say about pricing?",
        }
        d = _parse(raw, fallback_query="X")
        assert d.route == "SEARCH"
        assert d.filters["speaker_name"] == "Sarah"
        assert d.filters["date_from"] == "2026-04-01"
        assert d.search_query == "What did Sarah say about pricing?"
        assert d.scope_intent == {"needs_change": False, "reason": ""}

    def test_unknown_route_falls_back_to_SEARCH(self):
        raw = {"route": "UNKNOWN_ROUTE", "filters": {}, "search_query": "q"}
        d = _parse(raw, fallback_query="orig")
        assert d.route == "SEARCH"
        assert d.search_query == "q"

    def test_lowercase_route_normalised(self):
        raw = {"route": "search", "filters": {}, "search_query": "q"}
        assert _parse(raw, "x").route == "SEARCH"

    def test_empty_dict_falls_back(self):
        d = _parse({}, fallback_query="my question")
        assert d.route == "SEARCH"
        assert d.search_query == "my question"
        # All filters defaulted to None
        assert all(v is None for v in d.filters.values())

    def test_missing_filters_dict(self):
        d = _parse({"route": "SEARCH"}, fallback_query="X")
        assert all(v is None for v in d.filters.values())

    def test_garbage_input_falls_back_cleanly(self):
        d = _parse("not a dict", fallback_query="q")  # type: ignore[arg-type]
        assert d.route == "SEARCH"
        assert d.search_query == "q"

    def test_search_query_falls_back_when_blank(self):
        raw = {"route": "SEARCH", "filters": {}, "search_query": "   "}
        d = _parse(raw, fallback_query="real query")
        assert d.search_query == "real query"

    def test_out_of_30d_flag_passes_through(self):
        raw = {"route": "META", "filters": {}, "out_of_window": True, "search_query": "x"}
        assert _parse(raw, "x").out_of_window is True

    def test_scope_intent_normalises(self):
        raw = {"route": "SEARCH", "filters": {}, "scope_intent": {"needs_change": "yes",
               "reason": None}, "search_query": "x"}
        d = _parse(raw, "x")
        # Boolean-coerced; reason coerced to ""
        assert d.scope_intent == {"needs_change": True, "reason": ""}


# ── _fallback ─────────────────────────────────────────────────────────────────

def test_fallback_returns_search_with_raw_query():
    d = _fallback("anything")
    assert d.route == "SEARCH"
    assert d.search_query == "anything"
    assert d.out_of_window is False


# ── classify_query — happy + degraded paths ───────────────────────────────────

class TestClassifyQuery:
    @pytest.mark.asyncio
    async def test_passes_messages_with_today_in_system_prompt(self):
        llm = FakeLLM({"route": "META", "filters": {}, "search_query": "list meetings"})
        resolver = FakeResolver()
        await classify_query("list my meetings", llm=llm, speaker_resolver=resolver,
                             today=date(2026, 5, 4))
        # First message is system; should contain today's date string.
        assert llm.last_messages[0]["role"] == "system"
        assert "2026-05-04" in llm.last_messages[0]["content"]
        # User message is the query.
        assert llm.last_messages[1]["content"] == "list my meetings"

    @pytest.mark.asyncio
    async def test_uses_router_deployment(self):
        llm = FakeLLM({"route": "SEARCH", "filters": {}, "search_query": "q"})
        await classify_query("q", llm=llm, speaker_resolver=FakeResolver(),
                             today=date(2026, 5, 4))
        # The deployment string is whatever llm_for_router() returned — just
        # confirm it was set (not None / empty).
        assert llm.last_deployment

    @pytest.mark.asyncio
    async def test_speaker_resolution_one_match(self):
        llm = FakeLLM({
            "route": "SEARCH",
            "filters": {"speaker_name": "Ashish"},
            "search_query": "what did ashish say",
        })
        resolver = FakeResolver([
            SpeakerCandidate(name="Ashish Jaiswal", email="ashish@vrize.com", graph_id="g1"),
        ])
        d = await classify_query("what did Ashish say", llm=llm,
                                 speaker_resolver=resolver, today=date(2026, 5, 4))
        assert resolver.last_name == "Ashish"
        assert d.filters["speaker_graph_ids"] == ["g1"]
        assert d.filters["speaker_disambiguation_needed"] is False
        assert d.filters["speaker_candidates"] is None

    @pytest.mark.asyncio
    async def test_speaker_resolution_multi_match(self):
        llm = FakeLLM({
            "route": "SEARCH",
            "filters": {"speaker_name": "Ashish"},
            "search_query": "q",
        })
        resolver = FakeResolver([
            SpeakerCandidate(name="Ashish Jaiswal", email="aj@vrize.com", graph_id="g1"),
            SpeakerCandidate(name="Ashish Kumar",   email="ak@vrize.com", graph_id="g2"),
        ])
        d = await classify_query("Ashish?", llm=llm, speaker_resolver=resolver,
                                 today=date(2026, 5, 4))
        assert d.filters["speaker_graph_ids"] == ["g1", "g2"]
        assert d.filters["speaker_disambiguation_needed"] is True
        assert d.filters["speaker_candidates"] == [
            {"name": "Ashish Jaiswal", "email": "aj@vrize.com", "graph_id": "g1"},
            {"name": "Ashish Kumar",   "email": "ak@vrize.com", "graph_id": "g2"},
        ]

    @pytest.mark.asyncio
    async def test_speaker_resolution_no_match(self):
        llm = FakeLLM({
            "route": "SEARCH",
            "filters": {"speaker_name": "Nobody"},
            "search_query": "q",
        })
        d = await classify_query("Nobody said?", llm=llm,
                                 speaker_resolver=FakeResolver([]), today=date(2026, 5, 4))
        assert d.filters["speaker_graph_ids"] is None
        assert d.filters["speaker_disambiguation_needed"] is False

    @pytest.mark.asyncio
    async def test_no_speaker_in_query_no_resolver_call(self):
        llm = FakeLLM({"route": "META", "filters": {"speaker_name": None}, "search_query": "q"})
        resolver = FakeResolver()
        d = await classify_query("list meetings", llm=llm, speaker_resolver=resolver,
                                 today=date(2026, 5, 4))
        assert resolver.last_name is None
        # Defaults still populated for downstream consumers.
        assert d.filters["speaker_graph_ids"] is None
        assert d.filters["speaker_disambiguation_needed"] is False

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back(self):
        llm = FakeLLM(raise_on_call=RuntimeError("LLM down"))
        d = await classify_query("any query", llm=llm,
                                 speaker_resolver=FakeResolver(), today=date(2026, 5, 4))
        assert d.route == "SEARCH"
        assert d.search_query == "any query"

    @pytest.mark.asyncio
    async def test_resolver_failure_doesnt_crash(self):
        class BoomResolver:
            async def resolve(self, name):
                raise RuntimeError("DB down")

        llm = FakeLLM({
            "route": "SEARCH",
            "filters": {"speaker_name": "Sarah"},
            "search_query": "q",
        })
        d = await classify_query("Sarah said?", llm=llm,
                                 speaker_resolver=BoomResolver(), today=date(2026, 5, 4))
        assert d.route == "SEARCH"
        assert d.filters["speaker_graph_ids"] is None
        assert d.filters["speaker_disambiguation_needed"] is False


# ── Route coverage smoke tests ────────────────────────────────────────────────
# These confirm the parser accepts every defined route value. They do NOT test
# the LLM's classification quality — that's a manual / eval-harness concern.

@pytest.mark.parametrize("route", sorted(_VALID_ROUTES))
def test_every_valid_route_passes_through_parse(route):
    raw = {"route": route, "filters": {}, "search_query": "q"}
    assert _parse(raw, "x").route == route


# ── structured_intent hint validation ────────────────────────────────────────

class TestStructuredIntent:
    @pytest.mark.parametrize("intent", [
        "digest", "list_actions", "list_decisions", "list_followups",
    ])
    def test_valid_intents_pass_through(self, intent):
        raw = {
            "route": "STRUCTURED_DIRECT",
            "filters": {"structured_intent": intent},
            "search_query": "q",
        }
        assert _parse(raw, "x").filters["structured_intent"] == intent

    def test_invalid_intent_cleared_to_none(self):
        raw = {
            "route": "STRUCTURED_DIRECT",
            "filters": {"structured_intent": "bogus_value"},
            "search_query": "q",
        }
        assert _parse(raw, "x").filters["structured_intent"] is None

    def test_missing_intent_is_none(self):
        raw = {"route": "STRUCTURED_DIRECT", "filters": {}, "search_query": "q"}
        assert _parse(raw, "x").filters["structured_intent"] is None


# ── Fix B: server-side override of out_of_window ──────────────────────

class TestOutOf30Override:
    @pytest.mark.asyncio
    async def test_recent_date_overrides_llm_false_positive(self):
        """LLM says out_of_30=true for Apr 24, but today is May 5 → 11 days, in range."""
        llm = FakeLLM({
            "route": "META",
            "filters": {"date_from": "2026-04-24", "date_to": "2026-04-24"},
            "out_of_window": True,   # LLM lied
            "search_query": "did I attend on apr 24",
        })
        d = await classify_query("did I attend on Apr 24", llm=llm,
                                 speaker_resolver=FakeResolver(), today=date(2026, 5, 5))
        assert d.out_of_window is False

    @pytest.mark.asyncio
    async def test_old_date_correctly_flagged_even_when_llm_says_no(self):
        """LLM says out_of_30=false for Feb 1, but today is May 5 → 90+ days, out of range."""
        llm = FakeLLM({
            "route": "META",
            "filters": {"date_from": "2026-02-01", "date_to": "2026-02-28"},
            "out_of_window": False,   # LLM understated
            "search_query": "what did we discuss in Feb",
        })
        d = await classify_query("what about February", llm=llm,
                                 speaker_resolver=FakeResolver(), today=date(2026, 5, 5))
        assert d.out_of_window is True

    @pytest.mark.asyncio
    async def test_no_dates_extracted_flag_is_false(self):
        llm = FakeLLM({
            "route": "META", "filters": {}, "search_query": "list meetings",
            "out_of_window": True,   # LLM nonsense; we override
        })
        d = await classify_query("list my meetings", llm=llm,
                                 speaker_resolver=FakeResolver(), today=date(2026, 5, 5))
        assert d.out_of_window is False

    @pytest.mark.asyncio
    async def test_invalid_iso_date_does_not_trip_flag(self):
        llm = FakeLLM({
            "route": "META",
            "filters": {"date_from": "garbage", "date_to": None},
            "search_query": "x",
        })
        d = await classify_query("x", llm=llm,
                                 speaker_resolver=FakeResolver(), today=date(2026, 5, 5))
        assert d.out_of_window is False
