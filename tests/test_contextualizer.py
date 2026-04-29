"""
Tests for app/services/ingestion/contextualizer.py

Covers:
  - build_contextual_text: output contains meeting subject, date, speaker, text
  - build_contextual_text: output is richer than raw chunk text alone
  - build_contextual_text: handles missing/empty meeting metadata gracefully
  - contextualize_chunks: empty input returns empty list
  - contextualize_chunks: calls OpenAI with correct structure (mocked)
  - contextualize_chunks: returns one string per chunk in input order
  - contextualize_chunks: falls back to free-layer on LLM failure
  - contextualize_chunks: falls back when LLM returns wrong count
  - contextualize_chunks: each result is longer than the raw chunk text
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingestion.chunker import Chunk
from app.services.ingestion.contextualizer import (
    build_contextual_text,
    contextualize_chunks,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_chunk(index: int, text: str, speaker: str = "Raj Patel") -> Chunk:
    return Chunk(
        chunk_index=index,
        text=text,
        speaker=speaker,
        start_ms=index * 10_000,
        end_ms=(index + 1) * 10_000,
    )


SUBJECT = "Q4 Planning"
DATE = "2026-01-15"
SPEAKERS = ["Priyanka Sharma", "Raj Patel", "Sara Nguyen", "Marcus Lee"]

CHUNK_A = make_chunk(
    0,
    "I think we should accept Acme's terms. The 18 month price lock is within policy.",
    "Raj Patel",
)
CHUNK_B = make_chunk(
    1,
    "I agree with Raj. The onboarding timeline of 16 weeks is doable for our team.",
    "Sara Nguyen",
)


def _make_openai_response(contexts: list[str]) -> MagicMock:
    """Build a mock AsyncAzureOpenAI chat completion response."""
    msg = MagicMock()
    msg.content = json.dumps({"contexts": contexts})
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


# ── build_contextual_text ─────────────────────────────────────────────────────

class TestBuildContextualText:

    def test_contains_meeting_subject(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert SUBJECT in result

    def test_contains_meeting_date(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert DATE in result

    def test_contains_speaker_name(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert CHUNK_A.speaker in result

    def test_contains_chunk_text(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert CHUNK_A.text in result

    def test_contains_at_least_one_participant(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert any(s in result for s in SPEAKERS)

    def test_result_longer_than_raw_text(self):
        result = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        assert len(result) > len(CHUNK_A.text)

    def test_empty_subject_uses_fallback(self):
        result = build_contextual_text("", DATE, SPEAKERS, CHUNK_A)
        assert "Untitled" in result

    def test_empty_speakers_list(self):
        result = build_contextual_text(SUBJECT, DATE, [], CHUNK_A)
        assert SUBJECT in result  # Should not raise

    def test_truncates_speakers_to_five(self):
        many_speakers = [f"Person {i}" for i in range(10)]
        result = build_contextual_text(SUBJECT, DATE, many_speakers, CHUNK_A)
        # Only first 5 should appear; persons 5-9 should not all be present
        assert "Person 0" in result
        assert "Person 9" not in result

    def test_different_chunks_produce_different_output(self):
        r1 = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_A)
        r2 = build_contextual_text(SUBJECT, DATE, SPEAKERS, CHUNK_B)
        assert r1 != r2


# ── contextualize_chunks ──────────────────────────────────────────────────────

class TestContextualizeChunks:

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, [])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_one_string_per_chunk(self):
        chunks = [CHUNK_A, CHUNK_B]
        mock_response = _make_openai_response(["Context for chunk A.", "Context for chunk B."])

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_result_contains_llm_context_and_free_layer(self):
        """Each output should contain both the LLM topic sentence and the free-layer context."""
        chunks = [CHUNK_A]
        llm_topic = "Raj proposes accepting Acme deal with 18-month price lock."
        mock_response = _make_openai_response([llm_topic])

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        assert llm_topic in result[0]
        assert CHUNK_A.text in result[0]
        assert SUBJECT in result[0]

    @pytest.mark.asyncio
    async def test_each_result_longer_than_raw_chunk_text(self):
        chunks = [CHUNK_A, CHUNK_B]
        mock_response = _make_openai_response(
            ["Raj proposes Acme deal.", "Sara agrees on timeline."]
        )

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        for chunk, ctx in zip(chunks, result):
            assert len(ctx) > len(chunk.text), (
                f"Contextual text should be longer than raw text for chunk {chunk.chunk_index}"
            )

    @pytest.mark.asyncio
    async def test_openai_called_once_for_all_chunks(self):
        """One LLM call for all chunks — not one call per chunk."""
        chunks = [CHUNK_A, CHUNK_B]
        mock_response = _make_openai_response(["Topic A.", "Topic B."])

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            create_mock = AsyncMock(return_value=mock_response)
            mock_client.chat.completions.create = create_mock
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        assert create_mock.call_count == 1, (
            f"Expected 1 LLM call, got {create_mock.call_count}"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_free_layer_on_llm_exception(self):
        """If the LLM call raises any exception, return free-layer context without error."""
        chunks = [CHUNK_A, CHUNK_B]

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(
                side_effect=RuntimeError("Azure OpenAI timeout")
            )
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        assert len(result) == 2
        for chunk, ctx in zip(chunks, result):
            assert chunk.text in ctx
            assert SUBJECT in ctx

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_wrong_count(self):
        """If LLM returns fewer contexts than chunks, fall back to free-layer."""
        chunks = [CHUNK_A, CHUNK_B]
        mock_response = _make_openai_response(["Only one context."])  # Wrong: 2 expected

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        assert len(result) == 2
        # Fallback: raw chunk text must be present in each result.
        assert CHUNK_A.text in result[0]
        assert CHUNK_B.text in result[1]

    @pytest.mark.asyncio
    async def test_output_order_matches_input_order(self):
        """Results must be in the same order as the input chunks."""
        chunks = [
            make_chunk(0, "First chunk text about pricing.", "Alice"),
            make_chunk(1, "Second chunk text about timeline.", "Bob"),
            make_chunk(2, "Third chunk text about resources.", "Carol"),
        ]
        mock_response = _make_openai_response(["ctx1", "ctx2", "ctx3"])

        with patch(
            "app.services.ingestion.contextualizer._get_client"
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            with patch(
                "app.services.ingestion.contextualizer._llm_deployment",
                return_value="gpt-4o-mini",
            ):
                result = await contextualize_chunks(SUBJECT, DATE, SPEAKERS, chunks)

        for i, (chunk, ctx) in enumerate(zip(chunks, result)):
            assert chunk.text in ctx, (
                f"result[{i}] should contain text from chunk {i}"
            )
