"""
Tests for app/services/graph/meetings.py (MeetingsMixin)

All Graph HTTP calls are mocked — no real network requests are made.

Covers:
  - get_me: returns user profile, calls /me path
  - get_user_by_id: success, returns None on 404, re-raises non-404 errors
  - get_online_meeting: uses /me path, uses /users/{id} path with user_id
  - get_meeting_by_join_url: success, raises MeetingNotFoundError on empty,
    sends $filter param, uses /me and /users paths
"""

from unittest.mock import AsyncMock

import pytest

from app.services.graph.client import GraphClient
from app.services.graph.exceptions import GraphClientError, MeetingNotFoundError, TokenExpiredError


def _client() -> GraphClient:
    return GraphClient("fake-token")


# ── get_me ────────────────────────────────────────────────────────────────────

class TestGetMe:
    async def test_returns_user_profile(self):
        gc = _client()
        profile = {"id": "abc-123", "displayName": "John Doe", "mail": "john@co.com"}
        gc.get = AsyncMock(return_value=profile)

        result = await gc.get_me()
        assert result["id"] == "abc-123"

    async def test_calls_me_path(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"id": "x"})

        await gc.get_me()
        gc.get.assert_called_once_with("/me")

    async def test_returns_full_dict(self):
        gc = _client()
        profile = {"id": "u1", "displayName": "Alice", "mail": "alice@co.com",
                   "userPrincipalName": "alice@co.com"}
        gc.get = AsyncMock(return_value=profile)

        result = await gc.get_me()
        assert result == profile

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=TokenExpiredError("expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_me()

    async def test_propagates_graph_client_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("fail", status_code=500))

        with pytest.raises(GraphClientError):
            await gc.get_me()


# ── get_user_by_id ────────────────────────────────────────────────────────────

class TestGetUserById:
    async def test_returns_user_profile_on_success(self):
        gc = _client()
        profile = {"id": "abc", "displayName": "Jane Smith"}
        gc.get = AsyncMock(return_value=profile)

        result = await gc.get_user_by_id("abc")
        assert result["displayName"] == "Jane Smith"

    async def test_calls_correct_path(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"id": "abc"})

        await gc.get_user_by_id("abc-123")
        gc.get.assert_called_once_with("/users/abc-123")

    async def test_returns_none_on_404(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("not found", status_code=404))

        result = await gc.get_user_by_id("missing-id")
        assert result is None

    async def test_reraises_non_404_graph_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("forbidden", status_code=403))

        with pytest.raises(GraphClientError) as exc_info:
            await gc.get_user_by_id("some-id")
        assert exc_info.value.status_code == 403

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=TokenExpiredError("expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_user_by_id("some-id")


# ── get_online_meeting ────────────────────────────────────────────────────────

class TestGetOnlineMeeting:
    MEETING_ID = "meeting-graph-id-001"

    async def test_returns_meeting_dict(self):
        gc = _client()
        meeting = {"id": self.MEETING_ID, "subject": "Team Sync"}
        gc.get = AsyncMock(return_value=meeting)

        result = await gc.get_online_meeting(self.MEETING_ID)
        assert result["id"] == self.MEETING_ID

    async def test_uses_me_path_without_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"id": self.MEETING_ID})

        await gc.get_online_meeting(self.MEETING_ID)
        call_path = gc.get.call_args[0][0]
        assert call_path == f"/me/onlineMeetings/{self.MEETING_ID}"

    async def test_uses_users_path_with_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"id": self.MEETING_ID})

        await gc.get_online_meeting(self.MEETING_ID, user_id="user-guid")
        call_path = gc.get.call_args[0][0]
        assert call_path == f"/users/user-guid/onlineMeetings/{self.MEETING_ID}"

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=TokenExpiredError("expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_online_meeting(self.MEETING_ID)

    async def test_propagates_graph_client_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("server error", status_code=500))

        with pytest.raises(GraphClientError):
            await gc.get_online_meeting(self.MEETING_ID)


# ── get_meeting_by_join_url ───────────────────────────────────────────────────

class TestGetMeetingByJoinUrl:
    JOIN_URL = "https://teams.microsoft.com/l/meetup-join/abc123"
    MEETING_ID = "meeting-graph-id-001"

    async def test_returns_meeting_dict(self):
        gc = _client()
        meeting = {"id": self.MEETING_ID, "subject": "Standup"}
        gc.get = AsyncMock(return_value={"value": [meeting]})

        result = await gc.get_meeting_by_join_url(self.JOIN_URL)
        assert result["id"] == self.MEETING_ID

    async def test_raises_meeting_not_found_on_empty_value(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": []})

        with pytest.raises(MeetingNotFoundError):
            await gc.get_meeting_by_join_url(self.JOIN_URL)

    async def test_sends_filter_param(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": [{"id": "x"}]})

        await gc.get_meeting_by_join_url(self.JOIN_URL)
        call_kwargs = gc.get.call_args[1]
        assert "$filter" in call_kwargs.get("params", {})

    async def test_filter_contains_join_url(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": [{"id": "x"}]})

        await gc.get_meeting_by_join_url(self.JOIN_URL)
        params = gc.get.call_args[1].get("params", {})
        assert self.JOIN_URL in params["$filter"]

    async def test_uses_me_path_without_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": [{"id": "x"}]})

        await gc.get_meeting_by_join_url(self.JOIN_URL)
        call_path = gc.get.call_args[0][0]
        assert call_path == "/me/onlineMeetings"

    async def test_uses_users_path_with_user_id(self):
        gc = _client()
        gc.get = AsyncMock(return_value={"value": [{"id": "x"}]})

        await gc.get_meeting_by_join_url(self.JOIN_URL, user_id="u-99")
        call_path = gc.get.call_args[0][0]
        assert call_path == "/users/u-99/onlineMeetings"

    async def test_propagates_token_expired_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=TokenExpiredError("expired"))

        with pytest.raises(TokenExpiredError):
            await gc.get_meeting_by_join_url(self.JOIN_URL)

    async def test_propagates_graph_client_error(self):
        gc = _client()
        gc.get = AsyncMock(side_effect=GraphClientError("server error", status_code=500))

        with pytest.raises(GraphClientError):
            await gc.get_meeting_by_join_url(self.JOIN_URL)
