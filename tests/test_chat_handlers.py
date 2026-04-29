"""
Tests for chat route handlers and answer generator.

Covers:
  meta_handler:
    - returns empty list for empty authorized_ids
    - returns meeting list with participant names
    - applies date_from filter in SQL params
  structured_handler:
    - returns fell_through=True for empty authorized_ids
    - returns fell_through=True when DB returns no rows
    - merges multiple insight_type rows per meeting
    - returns fell_through=False when rows found
  search_handler:
    - returns empty list for empty authorized_ids
    - returns transcript dicts with correct source_type
    - applies speaker filter in SQL params
  hybrid_handler:
    - merges insights and chunks
    - insights come before chunks in result
  answer.generate_answer:
    - returns no-results message when handler_result is empty
    - calls LLM with correct route system prompt
    - truncates history at 6000 chars
    - formats META context correctly
    - formats SEARCH context with speaker and timestamp
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.answer import generate_answer, _build_context, _truncate_history


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _meta_item(mid: str | None = None) -> dict:
    return {
        "source_type": "metadata",
        "meeting_id": mid or str(uuid.uuid4()),
        "meeting_title": "Q4 Review",
        "meeting_date": "2026-04-22",
        "duration_minutes": 60,
        "participant_count": 3,
        "participants": ["Alice", "Bob", "Carol"],
    }


def _insight_item(mid: str | None = None) -> dict:
    return {
        "source_type": "insights",
        "meeting_id": mid or str(uuid.uuid4()),
        "meeting_title": "Sales Sync",
        "meeting_date": "2026-04-19",
        "summary": {"text": "Deal discussed."},
        "action_items": [{"owner": "Alice", "task": "Send deck"}],
    }


def _transcript_item(mid: str | None = None) -> dict:
    return {
        "source_type": "transcript",
        "meeting_id": mid or str(uuid.uuid4()),
        "meeting_title": "Sales Sync",
        "meeting_date": "2026-04-19",
        "speaker_name": "Bob",
        "timestamp_ms": 872000,
        "text": "We need to revisit enterprise pricing.",
        "similarity_score": 0.91,
    }


def _make_db_rows(*rows):
    return rows


# ── meta_handler ──────────────────────────────────────────────────────────────

class TestMetaHandler:

    @pytest.mark.asyncio
    async def test_empty_ids_returns_empty(self):
        from app.services.chat.meta_handler import handle_meta
        db = MagicMock()
        result = await handle_meta([], {}, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_metadata_dicts(self):
        from app.services.chat.meta_handler import handle_meta

        mid = uuid.uuid4()
        meeting_row = MagicMock()
        meeting_row.meeting_id = mid
        meeting_row.meeting_title = "Q4 Review"
        meeting_row.meeting_date = "2026-04-22"
        meeting_row.duration_minutes = 60
        meeting_row.participant_count = 2
        meeting_row.status = "ready"

        part_row = MagicMock()
        part_row.meeting_id = mid
        part_row.display_name = "Alice"

        call_count = 0

        async def fake_execute(stmt, params=None):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.__iter__ = MagicMock(return_value=iter([meeting_row]))
                result.__bool__ = MagicMock(return_value=True)
            else:
                result.__iter__ = MagicMock(return_value=iter([part_row]))
            return result

        db = MagicMock()
        db.execute = fake_execute

        result = await handle_meta([mid], {}, db)
        assert len(result) == 1
        assert result[0]["source_type"] == "metadata"
        assert result[0]["meeting_title"] == "Q4 Review"
        assert "Alice" in result[0]["participants"]


# ── structured_handler ────────────────────────────────────────────────────────

class TestStructuredHandler:

    @pytest.mark.asyncio
    async def test_empty_ids_falls_through(self):
        from app.services.chat.structured_handler import handle_structured
        db = MagicMock()
        result, fell = await handle_structured([], {}, db)
        assert result == []
        assert fell is True

    @pytest.mark.asyncio
    async def test_no_db_rows_falls_through(self):
        from app.services.chat.structured_handler import handle_structured

        async def fake_execute(stmt, params=None):
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([]))
            return r

        db = MagicMock()
        db.execute = fake_execute
        result, fell = await handle_structured([uuid.uuid4()], {}, db)
        assert result == []
        assert fell is True

    @pytest.mark.asyncio
    async def test_merges_insight_types_per_meeting(self):
        from app.services.chat.structured_handler import handle_structured

        mid = uuid.uuid4()

        def _row(itype, fields):
            r = MagicMock()
            r.meeting_id = mid
            r.insight_type = itype
            r.fields = fields
            r.meeting_title = "Board Meeting"
            r.meeting_date = "2026-04-10"
            return r

        rows = [
            _row("summary", {"text": "Overview."}),
            _row("action_items", [{"task": "Follow up"}]),
        ]

        async def fake_execute(stmt, params=None):
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter(rows))
            return r

        db = MagicMock()
        db.execute = fake_execute
        result, fell = await handle_structured([mid], {}, db)

        assert fell is False
        assert len(result) == 1
        assert result[0]["source_type"] == "insights"
        assert "summary" in result[0]
        assert "action_items" in result[0]

    @pytest.mark.asyncio
    async def test_returns_fell_through_false_when_rows_found(self):
        from app.services.chat.structured_handler import handle_structured

        mid = uuid.uuid4()
        row = MagicMock()
        row.meeting_id = mid
        row.insight_type = "summary"
        row.fields = {"text": "Meeting summary."}
        row.meeting_title = "Sync"
        row.meeting_date = "2026-04-15"

        async def fake_execute(stmt, params=None):
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([row]))
            return r

        db = MagicMock()
        db.execute = fake_execute
        _, fell = await handle_structured([mid], {}, db)
        assert fell is False


# ── search_handler ────────────────────────────────────────────────────────────

class TestSearchHandler:

    @pytest.mark.asyncio
    async def test_empty_ids_returns_empty(self):
        from app.services.chat.search_handler import handle_search
        db = MagicMock()
        result = await handle_search([0.1] * 1536, "query", [], {}, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_transcript_dicts(self):
        from app.services.chat.search_handler import handle_search

        mid = uuid.uuid4()
        row = MagicMock()
        row.meeting_id = mid
        row.meeting_title = "Sales Sync"
        row.meeting_date = "2026-04-19"
        row.speaker_name = "Bob"
        row.timestamp_ms = 60000
        row.text = "Pricing discussion."
        row.similarity_score = 0.88

        async def fake_execute(stmt, params=None):
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([row]))
            return r

        db = MagicMock()
        db.execute = fake_execute

        result = await handle_search([0.1] * 1536, "pricing", [mid], {}, db)
        assert len(result) == 1
        assert result[0]["source_type"] == "transcript"
        assert result[0]["speaker_name"] == "Bob"
        assert result[0]["similarity_score"] == 0.88

    @pytest.mark.asyncio
    async def test_speaker_filter_in_params(self):
        from app.services.chat.search_handler import handle_search

        captured_params: dict = {}

        async def fake_execute(stmt, params=None):
            captured_params.update(params or {})
            r = MagicMock()
            r.__iter__ = MagicMock(return_value=iter([]))
            return r

        db = MagicMock()
        db.execute = fake_execute
        await handle_search([0.1] * 1536, "query", [uuid.uuid4()], {"speaker": "Alice"}, db)
        assert captured_params.get("speaker") == "Alice"
        assert captured_params.get("speaker_pattern") == "%Alice%"


# ── hybrid_handler ────────────────────────────────────────────────────────────

class TestHybridHandler:

    @pytest.mark.asyncio
    async def test_merges_insights_and_chunks(self):
        from app.services.chat.hybrid_handler import handle_hybrid

        insight = _insight_item()
        chunk = _transcript_item()

        with patch("app.services.chat.hybrid_handler.handle_structured",
                   AsyncMock(return_value=([insight], False))), \
             patch("app.services.chat.hybrid_handler.handle_search",
                   AsyncMock(return_value=[chunk])):
            result = await handle_hybrid([0.1] * 1536, "query", [uuid.uuid4()], {}, MagicMock())

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_insights_before_chunks(self):
        from app.services.chat.hybrid_handler import handle_hybrid

        insight = _insight_item()
        chunk = _transcript_item()

        with patch("app.services.chat.hybrid_handler.handle_structured",
                   AsyncMock(return_value=([insight], False))), \
             patch("app.services.chat.hybrid_handler.handle_search",
                   AsyncMock(return_value=[chunk])):
            result = await handle_hybrid([0.1] * 1536, "query", [uuid.uuid4()], {}, MagicMock())

        assert result[0]["source_type"] == "insights"
        assert result[1]["source_type"] == "transcript"


# ── answer.generate_answer ────────────────────────────────────────────────────

class TestGenerateAnswer:

    @pytest.mark.asyncio
    async def test_empty_result_returns_no_results_message(self):
        answer = await generate_answer("query", "SEARCH", [], [])
        assert "couldn't find" in answer.lower() or "no" in answer.lower()

    @pytest.mark.asyncio
    async def test_calls_llm_with_route_system_prompt(self):
        captured = {}

        mock_client = AsyncMock()

        def _capture_create(**kwargs):
            captured.update(kwargs)
            msg = MagicMock()
            msg.content = "The answer."
            choice = MagicMock()
            choice.message = msg
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        mock_client.chat.completions.create = AsyncMock(side_effect=lambda **kw: _capture_create(**kw))

        with patch("app.services.ingestion.contextualizer._get_client", return_value=mock_client), \
             patch("app.services.chat.answer.get_settings") as mock_settings:
            mock_settings.return_value.AZURE_OPENAI_DEPLOYMENT_LLM = "gpt-4o"
            answer = await generate_answer(
                "What was discussed?", "SEARCH", [_transcript_item()], []
            )

        assert answer == "The answer."
        system_msg = captured["messages"][0]
        assert system_msg["role"] == "system"
        assert "transcript" in system_msg["content"].lower()

    def test_build_context_metadata(self):
        items = [_meta_item()]
        ctx = _build_context(items, "META")
        assert "Q4 Review" in ctx
        assert "Alice" in ctx

    def test_build_context_transcript_includes_timestamp(self):
        items = [_transcript_item()]
        ctx = _build_context(items, "SEARCH")
        assert "Bob" in ctx
        assert "14:32" in ctx  # 872000ms = 14:32
        assert "enterprise pricing" in ctx

    def test_build_context_empty_returns_fallback(self):
        ctx = _build_context([], "SEARCH")
        assert "No relevant content" in ctx

    def test_truncate_history_removes_oldest_first(self):
        history = [
            {"role": "user", "content": "x" * 3000},
            {"role": "assistant", "content": "y" * 3000},
            {"role": "user", "content": "short"},
        ]
        result = _truncate_history(history, max_chars=4000)
        assert result[-1]["content"] == "short"
        assert len(result) < 3

    def test_truncate_history_no_truncation_when_under_limit(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _truncate_history(history, max_chars=6000)
        assert len(result) == 2
