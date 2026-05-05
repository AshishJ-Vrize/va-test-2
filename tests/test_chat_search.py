"""Unit tests for the pure functions in app/db/helpers/chat_search.py.

Tests target:
  - _build_or_tsquery: stop-word + short-token filtering, OR construction
  - _rrf_merge:        RRF fusion math, dedup, sort by fused score
  - _round_robin_by_meeting: cross-meeting diversification, edge cases
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.db.helpers.chat_search import (
    _build_or_tsquery,
    _rrf_merge,
    _round_robin_by_meeting,
)
from app.services.chat.interfaces import RetrievedChunk


# ── Fixture builders ──────────────────────────────────────────────────────────

def _chunk(meeting_id: uuid.UUID, score: float = 0.0) -> RetrievedChunk:
    """Minimal RetrievedChunk for fusion / round-robin tests."""
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        meeting_id=meeting_id,
        meeting_title="X",
        meeting_date=datetime.utcnow(),
        speakers=["Ashish"],
        chunk_text=[{"n": "Ashish", "sn": "Ashish", "t": "test"}],
        start_ms=0,
        end_ms=1000,
        score=score,
    )


# ── _build_or_tsquery ─────────────────────────────────────────────────────────

class TestBuildOrTsQuery:
    def test_basic(self):
        assert _build_or_tsquery("Acme renewal pricing") == "acme | renewal | pricing"

    def test_strips_punctuation_and_lowercases(self):
        assert _build_or_tsquery("What's Acme's pricing?") == "acme | pricing"

    def test_drops_short_tokens(self):
        # "is", "ok" are stop words; "ok" is short anyway. "to", "be" are stop words.
        assert _build_or_tsquery("to be or not to be") is None

    def test_drops_stop_words(self):
        assert _build_or_tsquery("what was decided in the meeting") == "decided"

    def test_empty_returns_none(self):
        assert _build_or_tsquery("") is None
        assert _build_or_tsquery("   ") is None

    def test_only_stop_words_returns_none(self):
        assert _build_or_tsquery("the and or not") is None


# ── _rrf_merge ────────────────────────────────────────────────────────────────

class TestRRFMerge:
    def test_empty_inputs(self):
        assert _rrf_merge([], []) == []

    def test_one_list_only(self):
        m = uuid.uuid4()
        a, b, c = _chunk(m), _chunk(m), _chunk(m)
        result = _rrf_merge([a, b, c], [])
        assert [r.chunk_id for r in result] == [a.chunk_id, b.chunk_id, c.chunk_id]
        # Scores monotonically decreasing: 1/(60+1) > 1/(60+2) > 1/(60+3)
        assert result[0].score > result[1].score > result[2].score

    def test_consistent_high_rank_wins(self):
        """Chunk that's rank 1 in both lists should beat one that's only in one list."""
        m = uuid.uuid4()
        winner = _chunk(m)
        single = _chunk(m)
        result = _rrf_merge(
            vector=[winner, single],
            bm25=[winner],
        )
        ids = [r.chunk_id for r in result]
        assert ids[0] == winner.chunk_id  # appeared in both at high rank
        assert ids[1] == single.chunk_id

    def test_dedup_by_chunk_id(self):
        """Same chunk_id appears in both lists → one entry, summed score."""
        m = uuid.uuid4()
        c = _chunk(m)
        result = _rrf_merge([c], [c])
        assert len(result) == 1
        # Combined: 1/(60+1) + 1/(60+1) ≈ 0.0328
        assert result[0].score == pytest.approx(2 / 61, abs=1e-6)

    def test_disjoint_lists_both_included(self):
        m = uuid.uuid4()
        a = _chunk(m)  # vector only
        b = _chunk(m)  # bm25 only
        result = _rrf_merge([a], [b])
        assert {r.chunk_id for r in result} == {a.chunk_id, b.chunk_id}


# ── _round_robin_by_meeting ───────────────────────────────────────────────────

class TestRoundRobin:
    def test_empty_input(self):
        assert _round_robin_by_meeting([], top_k=10) == []

    def test_zero_top_k(self):
        m = uuid.uuid4()
        assert _round_robin_by_meeting([_chunk(m)], top_k=0) == []

    def test_single_meeting_acts_like_top_k(self):
        m = uuid.uuid4()
        chunks = [_chunk(m, score=1.0 - i * 0.1) for i in range(8)]
        result = _round_robin_by_meeting(chunks, top_k=5)
        # Single bucket — should preserve order, take first 5.
        assert [c.chunk_id for c in result] == [c.chunk_id for c in chunks[:5]]

    def test_three_meetings_diversifies(self):
        """3 meetings × 5 chunks each → top-10 must include all 3 meetings.

        Phase 2 DoD test fixture.
        """
        m1, m2, m3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # Construct fused order: m1 hogs the top, m2/m3 lower
        fused = (
            [_chunk(m1, score=1.0 - i * 0.01) for i in range(5)] +
            [_chunk(m2, score=0.5 - i * 0.01) for i in range(5)] +
            [_chunk(m3, score=0.3 - i * 0.01) for i in range(5)]
        )
        result = _round_robin_by_meeting(fused, top_k=10)
        meeting_ids_in_result = {c.meeting_id for c in result}
        assert meeting_ids_in_result == {m1, m2, m3}
        assert len(result) == 10

        # First 3 should be one from each meeting, in first-appearance order.
        assert [c.meeting_id for c in result[:3]] == [m1, m2, m3]

    def test_meeting_runs_out_skipped_gracefully(self):
        """One meeting has 1 chunk, others have many — no infinite loop."""
        m1, m2 = uuid.uuid4(), uuid.uuid4()
        fused = [_chunk(m1)] + [_chunk(m2) for _ in range(5)]
        result = _round_robin_by_meeting(fused, top_k=10)
        # Total available = 6; top_k=10. Should return all 6 without hang.
        assert len(result) == 6
        # Distribution: 1 from m1, 5 from m2.
        per_meeting = {}
        for c in result:
            per_meeting[c.meeting_id] = per_meeting.get(c.meeting_id, 0) + 1
        assert per_meeting[m1] == 1
        assert per_meeting[m2] == 5

    def test_first_appearance_order_preserved(self):
        """Meeting order in output cycles = first-appearance order in input."""
        m_a, m_b = uuid.uuid4(), uuid.uuid4()
        # m_b appears first in input
        fused = [_chunk(m_b), _chunk(m_a), _chunk(m_b), _chunk(m_a)]
        result = _round_robin_by_meeting(fused, top_k=4)
        # First cycle: m_b then m_a (first-appearance order)
        assert result[0].meeting_id == m_b
        assert result[1].meeting_id == m_a
        assert result[2].meeting_id == m_b
        assert result[3].meeting_id == m_a

    def test_internal_order_within_meeting_preserved(self):
        """Within a meeting, higher-RRF chunks come first."""
        m = uuid.uuid4()
        fused = [_chunk(m, score=0.9), _chunk(m, score=0.5), _chunk(m, score=0.1)]
        result = _round_robin_by_meeting(fused, top_k=3)
        assert [c.score for c in result] == [0.9, 0.5, 0.1]
