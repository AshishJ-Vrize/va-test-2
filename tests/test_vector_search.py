"""
Tests for app/db/helpers/vector_search.py

Covers:
  - _rrf_fuse: pure Python RRF fusion (no DB required)
  - hybrid_chunk_search: empty meeting_ids short-circuits before DB
  - hybrid_chunk_search: assembles ChunkHits in RRF-score order
  - hybrid_chunk_search: vector-only hits included when BM25 returns nothing
  - hybrid_chunk_search: all ChunkHit fields populated correctly
  - cross_meeting_search: assembles SummaryHits from DB rows
  - cross_meeting_search: empty result returns []
  - cross_meeting_search: None topics defaults to []
  - cross_meeting_search: DB called exactly once
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.helpers.vector_search import (
    ChunkHit,
    SummaryHit,
    _rrf_fuse,
    cross_meeting_search,
    hybrid_chunk_search,
)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _fake_embedding(seed: float = 0.1) -> list[float]:
    return [seed] * 1536


def _row(**kwargs):
    """Lightweight row substitute — attribute access via MagicMock."""
    obj = MagicMock()
    for k, v in kwargs.items():
        setattr(obj, k, v)
    return obj


def _make_execute_seq(*result_sets):
    """Return an async callable that yields successive result sets."""
    results = list(result_sets)
    call_idx = 0

    async def _execute(sql, params=None):
        nonlocal call_idx
        rs = results[call_idx] if call_idx < len(results) else []
        call_idx += 1
        return iter(rs)

    return _execute


# ── _rrf_fuse (pure Python — no DB) ──────────────────────────────────────────

class TestRrfFuse:

    def test_empty_inputs_returns_empty(self):
        assert _rrf_fuse({}, {}, rrf_k=60, pool=45, top_k=15) == []

    def test_vec_only_returns_results(self):
        vec = {"id-1": 1, "id-2": 2, "id-3": 3}
        result = _rrf_fuse(vec, {}, rrf_k=60, pool=45, top_k=3)
        assert len(result) == 3

    def test_top_vec_rank_scores_highest_when_only_vec(self):
        vec = {"id-1": 1, "id-2": 2, "id-3": 3}
        result = _rrf_fuse(vec, {}, rrf_k=60, pool=45, top_k=3)
        assert result[0][0] == "id-1"

    def test_scores_descending(self):
        vec = {"a": 1, "b": 2, "c": 3}
        bm25 = {"a": 3, "b": 1, "c": 2}
        result = _rrf_fuse(vec, bm25, rrf_k=60, pool=45, top_k=3)
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_id_in_both_rankers_scores_highest(self):
        vec = {"shared": 1, "vec-only": 2}
        bm25 = {"shared": 1, "bm25-only": 2}
        result = _rrf_fuse(vec, bm25, rrf_k=60, pool=45, top_k=3)
        assert result[0][0] == "shared"

    def test_top_k_limits_output(self):
        vec = {f"id-{i}": i for i in range(1, 11)}
        result = _rrf_fuse(vec, {}, rrf_k=60, pool=30, top_k=5)
        assert len(result) == 5

    def test_rrf_score_formula(self):
        """1/(k+1) + 1/(k+1) when both rankers rank an ID at position 1."""
        vec = {"x": 1}
        bm25 = {"x": 1}
        result = _rrf_fuse(vec, bm25, rrf_k=60, pool=3, top_k=1)
        _, score = result[0]
        expected = 1 / (60 + 1) + 1 / (60 + 1)
        assert abs(score - expected) < 1e-10

    def test_penalty_for_missing_id(self):
        """An ID absent from one ranker gets pool+1 penalty rank."""
        k, pool = 60, 3
        vec = {"v": 1}
        bm25 = {"b": 1}
        result = _rrf_fuse(vec, bm25, rrf_k=k, pool=pool, top_k=2)
        scores_map = dict(result)
        # Each appears in one ranker at rank 1, penalised in the other at pool+1
        expected = 1 / (k + 1) + 1 / (k + pool + 1)
        for cid in ("v", "b"):
            assert abs(scores_map[cid] - expected) < 1e-10


# ── hybrid_chunk_search ───────────────────────────────────────────────────────

class TestHybridChunkSearch:

    @pytest.mark.asyncio
    async def test_empty_meeting_ids_returns_empty_without_db_call(self):
        db = MagicMock()
        db.execute = AsyncMock()
        result = await hybrid_chunk_search(
            query_embedding=_fake_embedding(),
            query_text="test",
            meeting_ids=[],
            db=db,
        )
        assert result == []
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_chunk_hits_in_rrf_order(self):
        m_id, t_id = uuid.uuid4(), uuid.uuid4()
        c_a, c_b = uuid.uuid4(), uuid.uuid4()

        # A ranks 1st in both → should be result[0]
        vec_rows = [_row(chunk_id=c_a, rn=1), _row(chunk_id=c_b, rn=2)]
        bm25_rows = [_row(chunk_id=c_a, rn=1), _row(chunk_id=c_b, rn=2)]
        fetch_rows = [
            _row(chunk_id=c_a, meeting_id=m_id, meeting_subject="S", transcript_id=t_id,
                 speaker="Alice", text="Alice text", start_ms=0, end_ms=5000),
            _row(chunk_id=c_b, meeting_id=m_id, meeting_subject="S", transcript_id=t_id,
                 speaker="Bob", text="Bob text", start_ms=5000, end_ms=10000),
        ]

        db = MagicMock()
        db.execute = _make_execute_seq(vec_rows, bm25_rows, fetch_rows)

        result = await hybrid_chunk_search(
            query_embedding=_fake_embedding(),
            query_text="planning",
            meeting_ids=[m_id],
            db=db,
        )

        assert len(result) == 2
        assert str(result[0].chunk_id) == str(c_a)
        assert result[0].score >= result[1].score

    @pytest.mark.asyncio
    async def test_vec_only_hit_returned_when_no_bm25(self):
        m_id, t_id, c_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        fetch_rows = [
            _row(chunk_id=c_id, meeting_id=m_id, meeting_subject="X", transcript_id=t_id,
                 speaker="Carol", text="text", start_ms=0, end_ms=3000)
        ]
        db = MagicMock()
        db.execute = _make_execute_seq(
            [_row(chunk_id=c_id, rn=1)],  # vec
            [],                            # bm25 empty
            fetch_rows,
        )

        result = await hybrid_chunk_search(
            query_embedding=_fake_embedding(),
            query_text="unmatched",
            meeting_ids=[m_id],
            db=db,
        )

        assert len(result) == 1
        assert result[0].speaker == "Carol"

    @pytest.mark.asyncio
    async def test_all_chunk_hit_fields_populated(self):
        m_id, t_id, c_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        fetch_rows = [
            _row(chunk_id=c_id, meeting_id=m_id, meeting_subject="Sprint Review",
                 transcript_id=t_id, speaker="Dev Lead", text="We shipped feature X.",
                 start_ms=1000, end_ms=4000)
        ]
        db = MagicMock()
        db.execute = _make_execute_seq(
            [_row(chunk_id=c_id, rn=1)], [], fetch_rows
        )

        result = await hybrid_chunk_search(
            query_embedding=_fake_embedding(),
            query_text="feature shipped",
            meeting_ids=[m_id],
            db=db,
        )

        h = result[0]
        assert h.chunk_id == c_id
        assert h.meeting_id == m_id
        assert h.meeting_subject == "Sprint Review"
        assert h.transcript_id == t_id
        assert h.speaker == "Dev Lead"
        assert h.text == "We shipped feature X."
        assert h.start_ms == 1000
        assert h.end_ms == 4000
        assert isinstance(h.score, float) and h.score > 0


# ── cross_meeting_search ──────────────────────────────────────────────────────

class TestCrossMeetingSearch:

    @pytest.mark.asyncio
    async def test_returns_summary_hits(self):
        m_id = uuid.uuid4()
        rows = [
            _row(meeting_id=m_id, meeting_subject="Board Update",
                 meeting_date="2026-01-20",
                 summary_text="The board approved the Q1 budget.",
                 topics=["budget", "Q1"], similarity=0.87)
        ]
        db = MagicMock()
        db.execute = AsyncMock(return_value=iter(rows))

        result = await cross_meeting_search(
            query_embedding=_fake_embedding(),
            user_id=uuid.uuid4(),
            db=db,
        )

        assert len(result) == 1
        assert isinstance(result[0], SummaryHit)
        assert result[0].meeting_subject == "Board Update"
        assert result[0].score == pytest.approx(0.87)
        assert "budget" in result[0].topics

    @pytest.mark.asyncio
    async def test_empty_result_returns_empty_list(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=iter([]))
        result = await cross_meeting_search(
            query_embedding=_fake_embedding(),
            user_id=uuid.uuid4(),
            db=db,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_none_topics_defaults_to_empty_list(self):
        rows = [
            _row(meeting_id=uuid.uuid4(), meeting_subject="Standup",
                 meeting_date=None, summary_text="No blockers.", topics=None, similarity=0.75)
        ]
        db = MagicMock()
        db.execute = AsyncMock(return_value=iter(rows))

        result = await cross_meeting_search(
            query_embedding=_fake_embedding(),
            user_id=uuid.uuid4(),
            db=db,
        )

        assert result[0].topics == []
        assert result[0].meeting_date is None

    @pytest.mark.asyncio
    async def test_db_called_exactly_once(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=iter([]))

        await cross_meeting_search(
            query_embedding=_fake_embedding(),
            user_id=uuid.uuid4(),
            db=db,
        )

        assert db.execute.call_count == 1
