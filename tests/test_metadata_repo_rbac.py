"""Tests for MetadataRepoImpl.get_authorized_meeting_ids RBAC mode-switching.

Validates that the three configurable modes — date-only, count-only, and
intersection — produce the expected SQL fragments and parameter binding,
without requiring a real database. We mock the AsyncSession and capture the
SQL string + params that get passed in.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat.repos.metadata_repo import MetadataRepoImpl


def _make_repo() -> tuple[MetadataRepoImpl, AsyncMock]:
    """Return a repo whose execute() captures (sql_text, params)."""
    db = SimpleNamespace()
    db.execute = AsyncMock(return_value=iter([]))   # no rows
    return MetadataRepoImpl(db), db.execute


def _captured(execute_mock: AsyncMock) -> tuple[str, dict]:
    """Pull the rendered SQL string and params dict from the last execute call."""
    args, _ = execute_mock.call_args
    sql_obj, params = args
    return str(sql_obj), params


# ── Mode 1: only the date window ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_only_date_window_emits_date_clause_no_limit():
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="all", within_days=30, max_meetings=0,
    )
    sql, params = _captured(exec_mock)
    assert "meeting_date >= NOW()" in sql
    assert "LIMIT" not in sql.upper()
    assert params == {"gid": "g1", "days": 30}


# ── Mode 2: only the most-recent-N cap ────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_only_count_cap_emits_limit_no_date_clause():
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="all", within_days=0, max_meetings=30,
    )
    sql, params = _captured(exec_mock)
    assert "meeting_date >= NOW()" not in sql
    assert "LIMIT :max_meetings" in sql
    assert params == {"gid": "g1", "max_meetings": 30}


# ── Mode 3: both — intersection ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_intersection_emits_both_clauses():
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="all", within_days=30, max_meetings=10,
    )
    sql, params = _captured(exec_mock)
    assert "meeting_date >= NOW()" in sql
    assert "LIMIT :max_meetings" in sql
    assert params == {"gid": "g1", "days": 30, "max_meetings": 10}


# ── Both disabled — pure membership check ─────────────────────────────────────

@pytest.mark.asyncio
async def test_both_disabled_drops_both_clauses():
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="all", within_days=0, max_meetings=0,
    )
    sql, params = _captured(exec_mock)
    assert "meeting_date >= NOW()" not in sql
    assert "LIMIT" not in sql.upper()
    assert params == {"gid": "g1"}


# ── access_filter still composes alongside the new clauses ────────────────────

@pytest.mark.asyncio
async def test_access_filter_attended_combines_with_count_only_mode():
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="attended", within_days=0, max_meetings=5,
    )
    sql, _ = _captured(exec_mock)
    assert "mp.role IN ('organizer','attendee')" in sql
    assert "meeting_date >= NOW()" not in sql
    assert "LIMIT :max_meetings" in sql


# ── count_authorized_meetings — unbounded total ──────────────────────────────

@pytest.mark.asyncio
async def test_count_authorized_meetings_no_limit_clause():
    """The count query must NEVER include LIMIT — it's the unbounded total."""
    db = SimpleNamespace()
    fake_row = SimpleNamespace(total=87)
    fake_result = SimpleNamespace(first=lambda: fake_row)
    db.execute = AsyncMock(return_value=fake_result)
    repo = MetadataRepoImpl(db)

    n = await repo.count_authorized_meetings(
        graph_id="g1", access_filter="all", within_days=30,
    )
    sql, params = _captured(db.execute)
    assert "COUNT(DISTINCT m.id)" in sql
    assert "LIMIT" not in sql.upper()
    assert "max_meetings" not in params
    assert n == 87


@pytest.mark.asyncio
async def test_count_authorized_meetings_respects_within_days():
    db = SimpleNamespace()
    db.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: SimpleNamespace(total=0)))
    repo = MetadataRepoImpl(db)

    await repo.count_authorized_meetings(graph_id="g1", access_filter="all", within_days=0)
    sql, _ = _captured(db.execute)
    assert "meeting_date >= NOW()" not in sql      # date window disabled


@pytest.mark.asyncio
async def test_ordering_is_meeting_date_desc():
    """LIMIT must keep the most-recent meetings — ORDER BY emitted regardless of mode."""
    repo, exec_mock = _make_repo()
    await repo.get_authorized_meeting_ids(
        graph_id="g1", access_filter="all", within_days=30, max_meetings=10,
    )
    sql, _ = _captured(exec_mock)
    assert "ORDER BY m.meeting_date DESC" in sql
