"""
Tests for the private helpers and route logic in app/api/routes/ingest.py

All DB interactions are mocked — no real PostgreSQL needed.

Covers:
  - _parse_graph_dt: valid ISO 8601 with Z, with +00:00, None input, invalid string
  - _compute_duration: normal case, None start, None end, sub-minute rounds up to 1
  - _upsert_user: creates new user, updates existing user, falls back for empty fields
  - _upsert_meeting: creates new meeting, updates existing (preserves no implicit status)
  - _upsert_participant: creates new participant, no-op when already exists
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.routes.ingest import (
    _compute_duration,
    _parse_graph_dt,
    _upsert_meeting,
    _upsert_participant,
    _upsert_user,
)
from app.db.tenant.models import Meeting, MeetingParticipant, User


# ── DB mock helpers ───────────────────────────────────────────────────────────

def _make_db(scalar_result=None) -> MagicMock:
    """
    Return a mock AsyncSession where execute() is awaitable and returns a
    result whose scalar_one_or_none() returns `scalar_result`.
    """
    db = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    db.execute = AsyncMock(return_value=execute_result)
    db.flush = AsyncMock()
    return db


# ── _parse_graph_dt ───────────────────────────────────────────────────────────

class TestParseGraphDt:
    def test_parses_z_suffix(self):
        result = _parse_graph_dt("2026-03-25T10:31:24Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 25

    def test_parses_plus_00_00_suffix(self):
        result = _parse_graph_dt("2026-03-25T10:31:24+00:00")
        assert result is not None
        assert result.hour == 10
        assert result.minute == 31

    def test_returns_timezone_aware_datetime(self):
        result = _parse_graph_dt("2026-03-25T10:31:24Z")
        assert result.tzinfo is not None

    def test_returns_none_for_none_input(self):
        assert _parse_graph_dt(None) is None

    def test_returns_none_for_empty_string(self):
        assert _parse_graph_dt("") is None

    def test_returns_none_for_invalid_string(self):
        assert _parse_graph_dt("not-a-date") is None

    def test_parses_fractional_seconds(self):
        result = _parse_graph_dt("2026-03-25T10:31:24.8590375Z")
        assert result is not None
        assert result.second == 24

    def test_z_and_plus00_produce_same_result(self):
        r1 = _parse_graph_dt("2026-03-25T10:31:24Z")
        r2 = _parse_graph_dt("2026-03-25T10:31:24+00:00")
        assert r1 == r2


# ── _compute_duration ─────────────────────────────────────────────────────────

class TestComputeDuration:
    def _dt(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 3, 25, hour, minute, tzinfo=timezone.utc)

    def test_returns_correct_minutes(self):
        result = _compute_duration(self._dt(10), self._dt(11))
        assert result == 60

    def test_rounds_up_to_at_least_1(self):
        start = datetime(2026, 3, 25, 10, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 25, 10, 0, 30, tzinfo=timezone.utc)
        result = _compute_duration(start, end)
        assert result == 1

    def test_returns_none_when_start_is_none(self):
        assert _compute_duration(None, self._dt(11)) is None

    def test_returns_none_when_end_is_none(self):
        assert _compute_duration(self._dt(10), None) is None

    def test_returns_none_when_both_none(self):
        assert _compute_duration(None, None) is None

    def test_45_minute_meeting(self):
        result = _compute_duration(self._dt(10, 0), self._dt(10, 45))
        assert result == 45

    def test_result_is_integer(self):
        result = _compute_duration(self._dt(10), self._dt(11))
        assert isinstance(result, int)


# ── _upsert_user ─────────────────────────────────────────────────────────────

class TestUpsertUser:
    async def test_creates_new_user_when_not_found(self):
        db = _make_db(scalar_result=None)
        await _upsert_user(db, graph_id="gid-1", email="a@b.com", display_name="Alice")
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, User)
        assert added.graph_id == "gid-1"
        assert added.email == "a@b.com"
        assert added.display_name == "Alice"

    async def test_new_user_has_default_role_user(self):
        db = _make_db(scalar_result=None)
        await _upsert_user(db, graph_id="g1", email="x@y.com", display_name="X")
        added = db.add.call_args[0][0]
        assert added.system_role == "user"

    async def test_new_user_is_active(self):
        db = _make_db(scalar_result=None)
        await _upsert_user(db, graph_id="g1", email="x@y.com", display_name="X")
        added = db.add.call_args[0][0]
        assert added.is_active is True

    async def test_updates_existing_user_email(self):
        existing = MagicMock(spec=User)
        db = _make_db(scalar_result=existing)
        await _upsert_user(db, graph_id="g1", email="new@email.com", display_name="Old Name")
        assert existing.email == "new@email.com"

    async def test_updates_existing_user_display_name(self):
        existing = MagicMock(spec=User)
        db = _make_db(scalar_result=existing)
        await _upsert_user(db, graph_id="g1", email="a@b.com", display_name="New Name")
        assert existing.display_name == "New Name"

    async def test_does_not_add_when_user_exists(self):
        existing = MagicMock(spec=User)
        db = _make_db(scalar_result=existing)
        await _upsert_user(db, graph_id="g1", email="a@b.com", display_name="Alice")
        db.add.assert_not_called()

    async def test_returns_user_object(self):
        db = _make_db(scalar_result=None)
        result = await _upsert_user(db, graph_id="g1", email="a@b.com", display_name="Alice")
        assert isinstance(result, User)

    async def test_returns_existing_user_when_found(self):
        existing = MagicMock(spec=User)
        db = _make_db(scalar_result=existing)
        result = await _upsert_user(db, graph_id="g1", email="a@b.com", display_name="Alice")
        assert result is existing

    async def test_email_fallback_to_graph_id_when_empty(self):
        db = _make_db(scalar_result=None)
        await _upsert_user(db, graph_id="gid-fallback", email="", display_name="Name")
        added = db.add.call_args[0][0]
        assert added.email == "gid-fallback"

    async def test_display_name_fallback_when_empty(self):
        db = _make_db(scalar_result=None)
        await _upsert_user(db, graph_id="g1", email="a@b.com", display_name="")
        added = db.add.call_args[0][0]
        assert added.display_name == "Unknown"


# ── _upsert_meeting ───────────────────────────────────────────────────────────

class TestUpsertMeeting:
    BASE_DATE = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
    END_DATE = datetime(2026, 3, 25, 11, 0, tzinfo=timezone.utc)
    ORG_ID = uuid.uuid4()
    MEETING_GID = "meeting-graph-id-001"

    async def _call(self, db) -> Meeting:
        return await _upsert_meeting(
            db,
            meeting_graph_id=self.MEETING_GID,
            organizer_id=self.ORG_ID,
            subject="Weekly Standup",
            meeting_date=self.BASE_DATE,
            meeting_end_date=self.END_DATE,
            duration_minutes=60,
            join_url="https://teams.microsoft.com/join/abc",
        )

    async def test_creates_new_meeting_when_not_found(self):
        db = _make_db(scalar_result=None)
        await self._call(db)
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, Meeting)

    async def test_new_meeting_has_pending_status(self):
        db = _make_db(scalar_result=None)
        await self._call(db)
        added = db.add.call_args[0][0]
        assert added.status == "pending"

    async def test_new_meeting_source_is_manual(self):
        db = _make_db(scalar_result=None)
        await self._call(db)
        added = db.add.call_args[0][0]
        assert added.ingestion_source == "manual"

    async def test_new_meeting_has_all_fields(self):
        db = _make_db(scalar_result=None)
        await self._call(db)
        added = db.add.call_args[0][0]
        assert added.meeting_graph_id == self.MEETING_GID
        assert added.meeting_subject == "Weekly Standup"
        assert added.duration_minutes == 60

    async def test_updates_existing_meeting_subject(self):
        existing = MagicMock(spec=Meeting)
        db = _make_db(scalar_result=existing)
        await self._call(db)
        assert existing.meeting_subject == "Weekly Standup"

    async def test_updates_existing_meeting_duration(self):
        existing = MagicMock(spec=Meeting)
        db = _make_db(scalar_result=existing)
        await self._call(db)
        assert existing.duration_minutes == 60

    async def test_does_not_add_when_meeting_exists(self):
        existing = MagicMock(spec=Meeting)
        db = _make_db(scalar_result=existing)
        await self._call(db)
        db.add.assert_not_called()

    async def test_returns_meeting_object(self):
        db = _make_db(scalar_result=None)
        result = await self._call(db)
        assert isinstance(result, Meeting)

    async def test_returns_existing_when_found(self):
        existing = MagicMock(spec=Meeting)
        db = _make_db(scalar_result=existing)
        result = await self._call(db)
        assert result is existing


# ── _upsert_participant ───────────────────────────────────────────────────────

class TestUpsertParticipant:
    MEETING_ID = uuid.uuid4()
    USER_ID = uuid.uuid4()

    async def test_creates_participant_when_not_found(self):
        db = _make_db(scalar_result=None)
        await _upsert_participant(db, meeting_id=self.MEETING_ID, user_id=self.USER_ID, role="organizer")
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert isinstance(added, MeetingParticipant)

    async def test_new_participant_has_correct_role(self):
        db = _make_db(scalar_result=None)
        await _upsert_participant(db, meeting_id=self.MEETING_ID, user_id=self.USER_ID, role="attendee")
        added = db.add.call_args[0][0]
        assert added.role == "attendee"

    async def test_new_participant_has_correct_ids(self):
        db = _make_db(scalar_result=None)
        await _upsert_participant(db, meeting_id=self.MEETING_ID, user_id=self.USER_ID, role="organizer")
        added = db.add.call_args[0][0]
        assert added.meeting_id == self.MEETING_ID
        assert added.user_id == self.USER_ID

    async def test_no_op_when_participant_already_exists(self):
        existing = MagicMock(spec=MeetingParticipant)
        db = _make_db(scalar_result=existing)
        await _upsert_participant(db, meeting_id=self.MEETING_ID, user_id=self.USER_ID, role="organizer")
        db.add.assert_not_called()

    async def test_granted_by_is_none_by_default(self):
        db = _make_db(scalar_result=None)
        await _upsert_participant(db, meeting_id=self.MEETING_ID, user_id=self.USER_ID, role="granted")
        added = db.add.call_args[0][0]
        assert added.granted_by is None

    async def test_granted_by_can_be_set(self):
        granter_id = uuid.uuid4()
        db = _make_db(scalar_result=None)
        await _upsert_participant(
            db,
            meeting_id=self.MEETING_ID,
            user_id=self.USER_ID,
            role="granted",
            granted_by=granter_id,
        )
        added = db.add.call_args[0][0]
        assert added.granted_by == granter_id
