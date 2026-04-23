"""
Tests for app/services/graph/transcripts.py (TranscriptsMixin)

All Graph HTTP calls are mocked — no real network requests are made.

Covers:
  - get_transcripts: returns list, returns empty list, uses correct path,
    respects user_id parameter
  - get_transcript_content: returns VTT string, calls get_text with correct
    path and $format param, uses 60s timeout, respects user_id parameter
"""

from unittest.mock import AsyncMock

import pytest

from app.services.graph.client import GraphClient
from app.services.graph.exceptions import GraphClientError, TokenExpiredError


def _client() -> GraphClient:
    return GraphClient("fake-token")


# ── get_transcripts ───────────────────────────────────────────────────────────

class TestGetTranscripts:
    MEETING_ID = "meeting-graph-id-123"

    async def test_returns_transcript_list(self):
        gc = _client()
        transcripts = [
            {"id": "t1", "createdDateTime": "2026-03-25T10:31:24Z"},
            {"id": "t2", "createdDateTime": "2026-03-25T11:00:00Z"},
        ]
        gc.get = AsyncMock(return_value={"value": transcripts})

        result = await gc.get_transcripts(self.MEETING_ID)
        assert len(result) == 2
        assert result[0]["id"] == "t1"

    async def test_returns_empty_list_when_none_available(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": []})

        result = await gc.get_transcripts(self.MEETING_ID)
        assert result == []

    async def test_returns_empty_list_on_missing_value_key(self):
        gc = _client()
        gc.get = AsyncMock(return_value={})

        result = await gc.get_transcripts(self.MEETING_ID)
        assert result == []

    async def test_uses_me_path_without_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": []})

        await gc.get_transcripts(self.MEETING_ID)
        call_path = gc.get.call_args[0][0]
        assert call_path == f"/me/onlineMeetings/{self.MEETING_ID}/transcripts"

    async def test_uses_users_path_with_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": []})

        await gc.get_transcripts(self.MEETING_ID, user_id="organizer-guid")
        call_path = gc.get.call_args[0][0]
        assert call_path == f"/users/organizer-guid/onlineMeetings/{self.MEETING_ID}/transcripts"

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=TokenExpiredError("token expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_transcripts(self.MEETING_ID)

    async def test_propagates_graph_client_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("server error", status_code=500))

        with pytest.raises(GraphClientError):
            await gc.get_transcripts(self.MEETING_ID)

    async def test_returns_list_type(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": [{"id": "t1"}]})

        result = await gc.get_transcripts(self.MEETING_ID)
        assert isinstance(result, list)


# ── get_transcript_content ────────────────────────────────────────────────────

class TestGetTranscriptContent:
    MEETING_ID = "meeting-graph-id-123"
    TRANSCRIPT_ID = "transcript-id-456"
    SAMPLE_VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v John>Hello.\n"

    async def test_returns_vtt_string(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        result = await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
        assert result == self.SAMPLE_VTT

    async def test_returns_string_type(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        result = await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
        assert isinstance(result, str)

    async def test_calls_correct_path(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
        call_path = gc.get_text.call_args[0][0]
        expected = (
            f"/me/onlineMeetings/{self.MEETING_ID}"
            f"/transcripts/{self.TRANSCRIPT_ID}/content"
        )
        assert call_path == expected

    async def test_uses_users_path_with_user_id(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID, user_id="u-123")
        call_path = gc.get_text.call_args[0][0]
        expected = (
            f"/users/u-123/onlineMeetings/{self.MEETING_ID}"
            f"/transcripts/{self.TRANSCRIPT_ID}/content"
        )
        assert call_path == expected

    async def test_sends_vtt_format_param(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
        call_kwargs = gc.get_text.call_args[1]
        assert call_kwargs.get("params") == {"$format": "text/vtt"}

    async def test_uses_60s_timeout(self):
        gc = _client()
        gc.get_text = AsyncMock(return_value=self.SAMPLE_VTT)

        await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
        call_kwargs = gc.get_text.call_args[1]
        assert call_kwargs.get("timeout") == 60.0

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get_text = AsyncMock(side_effect=TokenExpiredError("expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)

    async def test_propagates_graph_client_error(self):
        gc = _client()
        gc.get_text = AsyncMock(
            side_effect=GraphClientError("not found", status_code=404)
        )

        with pytest.raises(GraphClientError):
            await gc.get_transcript_content(self.MEETING_ID, self.TRANSCRIPT_ID)
