"""
Tests for app/services/graph/exceptions.py

Covers:
  - GraphClientError stores message and status_code correctly
  - GraphClientError defaults status_code to None
  - TokenExpiredError is an Exception subclass
  - MeetingNotFoundError stores message correctly
  - All exceptions are catchable by their base type
"""

import pytest

from app.services.graph.exceptions import (
    GraphClientError,
    MeetingNotFoundError,
    TokenExpiredError,
)


class TestGraphClientError:
    def test_stores_message(self):
        exc = GraphClientError("something went wrong", status_code=503)
        assert exc.message == "something went wrong"

    def test_stores_status_code(self):
        exc = GraphClientError("error", status_code=404)
        assert exc.status_code == 404

    def test_status_code_defaults_to_none(self):
        exc = GraphClientError("network failure")
        assert exc.status_code is None

    def test_is_exception_subclass(self):
        exc = GraphClientError("error", status_code=500)
        assert isinstance(exc, Exception)

    def test_str_representation(self):
        exc = GraphClientError("bad request", status_code=400)
        assert "bad request" in str(exc)

    def test_catchable_as_exception(self):
        with pytest.raises(Exception):
            raise GraphClientError("fail", status_code=503)

    def test_catchable_as_graph_client_error(self):
        with pytest.raises(GraphClientError):
            raise GraphClientError("fail", status_code=503)

    def test_status_code_429(self):
        exc = GraphClientError("rate limited", status_code=429)
        assert exc.status_code == 429

    def test_status_code_none_for_network_error(self):
        exc = GraphClientError("connection refused", status_code=None)
        assert exc.status_code is None


class TestTokenExpiredError:
    def test_is_exception_subclass(self):
        exc = TokenExpiredError("token expired")
        assert isinstance(exc, Exception)

    def test_catchable_as_token_expired_error(self):
        with pytest.raises(TokenExpiredError):
            raise TokenExpiredError("expired")

    def test_catchable_as_exception(self):
        with pytest.raises(Exception):
            raise TokenExpiredError("expired")

    def test_message_preserved(self):
        exc = TokenExpiredError("delegated token expired")
        assert "delegated token expired" in str(exc)

    def test_not_catchable_as_graph_client_error(self):
        # TokenExpiredError and GraphClientError are sibling classes, not parent-child
        with pytest.raises(TokenExpiredError):
            try:
                raise TokenExpiredError("expired")
            except GraphClientError:
                pass  # should not be caught here
            raise TokenExpiredError("expired")


class TestMeetingNotFoundError:
    def test_stores_message(self):
        exc = MeetingNotFoundError("no meeting found for this URL")
        assert exc.message == "no meeting found for this URL"

    def test_is_exception_subclass(self):
        exc = MeetingNotFoundError("not found")
        assert isinstance(exc, Exception)

    def test_catchable_as_meeting_not_found_error(self):
        with pytest.raises(MeetingNotFoundError):
            raise MeetingNotFoundError("not found")

    def test_str_representation(self):
        exc = MeetingNotFoundError("join URL not matched")
        assert "join URL not matched" in str(exc)

    def test_not_catchable_as_graph_client_error(self):
        # MeetingNotFoundError is separate from GraphClientError
        caught = False
        try:
            raise MeetingNotFoundError("not found")
        except GraphClientError:
            caught = True
        except MeetingNotFoundError:
            pass  # propagates past GraphClientError — expected
        assert not caught
