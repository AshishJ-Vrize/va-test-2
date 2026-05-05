"""Unit tests for app/services/chat/scope.py."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.chat.interfaces import RouterDecision
from app.services.chat.scope import (
    NarrowResult,
    detect_scope_change_suggestion,
    narrow_within_scope,
)


# ── Fake MetadataRepo ─────────────────────────────────────────────────────────

class FakeMetadataRepo:
    """Drives narrow_within_scope() without a database.

    Configure with `title_to_ids` and `date_to_ids` mappings to control what
    `search_by_title` and `get_meetings_in_date_range` return for each call.
    """

    def __init__(
        self,
        *,
        title_hits: list[uuid.UUID] | None = None,
        date_hits: list[uuid.UUID] | None = None,
    ) -> None:
        self._title_hits = list(title_hits or [])
        self._date_hits = list(date_hits or [])
        self.search_by_title_calls: list[dict[str, Any]] = []
        self.get_meetings_in_date_range_calls: list[dict[str, Any]] = []

    async def search_by_title(self, candidate_titles, allowed_meeting_ids=None):
        self.search_by_title_calls.append({
            "candidate_titles": list(candidate_titles or []),
            "allowed_meeting_ids": list(allowed_meeting_ids or []),
        })
        return list(self._title_hits)

    async def get_meetings_in_date_range(self, date_from, date_to, allowed_meeting_ids=None):
        self.get_meetings_in_date_range_calls.append({
            "date_from": date_from,
            "date_to": date_to,
            "allowed_meeting_ids": list(allowed_meeting_ids or []),
        })
        return list(self._date_hits)

    # Unused by these tests but required to satisfy MetadataRepo Protocol.
    async def get_meetings(self, meeting_ids):
        return []

    async def get_participants(self, meeting_ids):
        return {}


# ── narrow_within_scope: branch coverage ──────────────────────────────────────

class TestNarrowWithinScope:
    @pytest.mark.asyncio
    async def test_no_filters_no_narrowing(self):
        sel = [uuid.uuid4(), uuid.uuid4()]
        repo = FakeMetadataRepo()
        result = await narrow_within_scope(
            selected_ids=sel, requested_titles=None, date_from=None, date_to=None,
            tenant_30d_meeting_ids=sel, metadata_repo=repo,
        )
        assert result.narrowed is False
        assert result.matched_ids == sel
        assert result.dropped_ids == []
        assert result.extra_ids == []
        # Repo not called when nothing to narrow on.
        assert repo.search_by_title_calls == []
        assert repo.get_meetings_in_date_range_calls == []

    @pytest.mark.asyncio
    async def test_title_request_all_inside_selection(self):
        # Two meetings selected; user names one. No extras.
        m1 = uuid.uuid4()
        m2 = uuid.uuid4()
        repo = FakeMetadataRepo(title_hits=[m1])
        result = await narrow_within_scope(
            selected_ids=[m1, m2],
            requested_titles=["Acme review"], date_from=None, date_to=None,
            tenant_30d_meeting_ids=[m1, m2],
            metadata_repo=repo,
        )
        assert result.narrowed is True
        assert result.matched_ids == [m1]
        assert result.dropped_ids == [m2]
        assert result.extra_ids == []

    @pytest.mark.asyncio
    async def test_title_request_some_outside_selection(self):
        # User selected only m1. Title query also matches m2 (NOT selected).
        m1 = uuid.uuid4()
        m2 = uuid.uuid4()
        repo = FakeMetadataRepo(title_hits=[m1, m2])
        result = await narrow_within_scope(
            selected_ids=[m1],
            requested_titles=["renewal"], date_from=None, date_to=None,
            tenant_30d_meeting_ids=[m1, m2],
            metadata_repo=repo,
        )
        assert result.narrowed is True
        assert result.matched_ids == [m1]
        assert result.dropped_ids == []
        assert result.extra_ids == [m2]

    @pytest.mark.asyncio
    async def test_title_request_all_outside_selection(self):
        # User selected m1; title query only matches m2 (NOT selected).
        m1 = uuid.uuid4()
        m2 = uuid.uuid4()
        repo = FakeMetadataRepo(title_hits=[m2])
        result = await narrow_within_scope(
            selected_ids=[m1],
            requested_titles=["other"], date_from=None, date_to=None,
            tenant_30d_meeting_ids=[m1, m2],
            metadata_repo=repo,
        )
        assert result.narrowed is True
        assert result.matched_ids == []
        assert result.dropped_ids == [m1]
        assert result.extra_ids == [m2]

    @pytest.mark.asyncio
    async def test_date_only_request(self):
        m1, m2 = uuid.uuid4(), uuid.uuid4()
        repo = FakeMetadataRepo(date_hits=[m1])
        result = await narrow_within_scope(
            selected_ids=[m1, m2],
            requested_titles=None, date_from="2026-04-01", date_to="2026-04-30",
            tenant_30d_meeting_ids=[m1, m2],
            metadata_repo=repo,
        )
        assert result.narrowed is True
        assert result.matched_ids == [m1]
        assert result.dropped_ids == [m2]

    @pytest.mark.asyncio
    async def test_title_and_date_intersected(self):
        # Title hits {m1, m2}; date hits {m2, m3}. AND → {m2}.
        m1, m2, m3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        repo = FakeMetadataRepo(title_hits=[m1, m2], date_hits=[m2, m3])
        result = await narrow_within_scope(
            selected_ids=[m1, m2, m3],
            requested_titles=["foo"], date_from="2026-04-01", date_to="2026-04-30",
            tenant_30d_meeting_ids=[m1, m2, m3],
            metadata_repo=repo,
        )
        assert result.narrowed is True
        assert result.matched_ids == [m2]
        assert set(result.dropped_ids) == {m1, m3}


# ── detect_scope_change_suggestion ────────────────────────────────────────────

def _decision(reason: str = "") -> RouterDecision:
    return RouterDecision(
        route="SEARCH",
        filters={},
        scope_intent={"needs_change": False, "reason": reason},
        out_of_window=False,
        search_query="x",
    )


class TestDetectScopeChangeSuggestion:
    def test_no_extras_no_suggestion(self):
        sel = [uuid.uuid4()]
        nr = NarrowResult(matched_ids=sel, narrowed=True)
        s = detect_scope_change_suggestion(
            narrow_result=nr, router_decision=_decision(), selected_ids=sel,
        )
        assert s.surface is False

    def test_extras_with_narrowing_surfaces(self):
        m1 = uuid.uuid4()
        m_extra = uuid.uuid4()
        nr = NarrowResult(
            matched_ids=[m1], dropped_ids=[], extra_ids=[m_extra], narrowed=True,
        )
        s = detect_scope_change_suggestion(
            narrow_result=nr, router_decision=_decision(), selected_ids=[m1],
        )
        assert s.surface is True
        assert m1 in s.new_meeting_ids and m_extra in s.new_meeting_ids
        assert "1 matching meeting" in s.reason

    def test_extras_without_narrowing_uses_router_reason(self):
        m1 = uuid.uuid4()
        m_extra = uuid.uuid4()
        nr = NarrowResult(
            matched_ids=[m1], extra_ids=[m_extra], narrowed=False,
        )
        s = detect_scope_change_suggestion(
            narrow_result=nr,
            router_decision=_decision(reason="Wider scope might help."),
            selected_ids=[m1],
        )
        assert s.surface is True
        assert s.reason == "Wider scope might help."

    def test_multiple_extras_pluralised(self):
        m1 = uuid.uuid4()
        extras = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        nr = NarrowResult(matched_ids=[m1], extra_ids=extras, narrowed=True)
        s = detect_scope_change_suggestion(
            narrow_result=nr, router_decision=_decision(), selected_ids=[m1],
        )
        assert s.surface is True
        assert "3 matching meetings" in s.reason
