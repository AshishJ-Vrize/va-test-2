"""
Tests for app/db/registry.py (TenantRegistry + CachedTenant)

All SQLAlchemy sessions are mocked — no real DB connections needed.

Covers:
  - CachedTenant: immutable (frozen dataclass), stores all fields
  - TenantRegistry.load_all: queries DB, replaces cache atomically
  - TenantRegistry.get: returns cached tenant, None for unknown tid
  - TenantRegistry.put: adds to cache, overwrites existing
  - TenantRegistry.invalidate: removes from cache, no-op for unknown tid
  - TenantRegistry.refresh_one: found in DB updates cache, not found removes from cache
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.registry import CachedTenant, TenantRegistry


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tenant_row(
    ms_tenant_id: str = "tid-abc",
    org_name: str = "acme",
    status: str = "active",
    plan: str = "pro",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.ms_tenant_id = ms_tenant_id
    row.org_name = org_name
    row.db_host = "pg-acme.postgres.database.azure.com"
    row.db_region = "eastus"
    row.status = status
    row.plan = plan
    row.max_users = 50
    return row


def _make_session(rows: list) -> MagicMock:
    """Return a mock AsyncSession whose execute().scalars().all() returns rows."""
    session = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _make_session_filter(row_or_none) -> MagicMock:
    """Return a mock AsyncSession whose execute().scalar_one_or_none() returns row_or_none."""
    session = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = row_or_none
    session.execute = AsyncMock(return_value=execute_result)
    return session


# ── CachedTenant ──────────────────────────────────────────────────────────────

class TestCachedTenant:
    def test_stores_all_fields(self):
        ct = CachedTenant(
            id="tenant-uuid",
            org_name="acme",
            ms_tenant_id="tid-abc",
            db_host="pg.example.com",
            db_region="eastus",
            status="active",
            plan="pro",
            max_users=100,
        )
        assert ct.id == "tenant-uuid"
        assert ct.org_name == "acme"
        assert ct.ms_tenant_id == "tid-abc"
        assert ct.plan == "pro"
        assert ct.max_users == 100

    def test_is_immutable(self):
        ct = CachedTenant(
            id="x", org_name="acme", ms_tenant_id="tid",
            db_host="h", db_region="r", status="active", plan="pro", max_users=10,
        )
        from dataclasses import FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            ct.status = "suspended"  # type: ignore[misc]


# ── TenantRegistry.load_all ───────────────────────────────────────────────────

class TestLoadAll:
    async def test_loads_all_tenants_from_db(self):
        rows = [_make_tenant_row("tid-1"), _make_tenant_row("tid-2")]
        session = _make_session(rows)
        registry = TenantRegistry()
        await registry.load_all(session)
        assert registry.get("tid-1") is not None
        assert registry.get("tid-2") is not None

    async def test_replaces_existing_cache(self):
        registry = TenantRegistry()
        old_row = _make_tenant_row("tid-old")
        await registry.load_all(_make_session([old_row]))
        assert registry.get("tid-old") is not None

        new_row = _make_tenant_row("tid-new")
        await registry.load_all(_make_session([new_row]))

        assert registry.get("tid-old") is None
        assert registry.get("tid-new") is not None

    async def test_empty_db_clears_cache(self):
        registry = TenantRegistry()
        await registry.load_all(_make_session([_make_tenant_row("tid-1")]))
        assert registry.get("tid-1") is not None

        await registry.load_all(_make_session([]))
        assert registry.get("tid-1") is None

    async def test_returns_cached_tenant_type(self):
        rows = [_make_tenant_row("tid-x")]
        registry = TenantRegistry()
        await registry.load_all(_make_session(rows))
        result = registry.get("tid-x")
        assert isinstance(result, CachedTenant)

    async def test_preserves_org_name(self):
        row = _make_tenant_row("tid-1", org_name="globex")
        registry = TenantRegistry()
        await registry.load_all(_make_session([row]))
        assert registry.get("tid-1").org_name == "globex"


# ── TenantRegistry.get ────────────────────────────────────────────────────────

class TestGet:
    def test_returns_none_for_unknown_tid(self):
        registry = TenantRegistry()
        assert registry.get("completely-unknown") is None

    async def test_returns_cached_tenant(self):
        row = _make_tenant_row("tid-abc")
        registry = TenantRegistry()
        await registry.load_all(_make_session([row]))
        result = registry.get("tid-abc")
        assert result is not None
        assert result.ms_tenant_id == "tid-abc"


# ── TenantRegistry.put ────────────────────────────────────────────────────────

class TestPut:
    def test_adds_tenant_to_cache(self):
        registry = TenantRegistry()
        row = _make_tenant_row("tid-new")
        registry.put(row)
        result = registry.get("tid-new")
        assert result is not None
        assert result.ms_tenant_id == "tid-new"

    def test_overwrites_existing_entry(self):
        registry = TenantRegistry()
        row = _make_tenant_row("tid-1", status="active")
        registry.put(row)
        assert registry.get("tid-1").status == "active"

        updated_row = _make_tenant_row("tid-1", status="suspended")
        registry.put(updated_row)
        assert registry.get("tid-1").status == "suspended"

    def test_converts_id_to_string(self):
        registry = TenantRegistry()
        row = _make_tenant_row("tid-1")
        registry.put(row)
        result = registry.get("tid-1")
        assert isinstance(result.id, str)


# ── TenantRegistry.invalidate ─────────────────────────────────────────────────

class TestInvalidate:
    def test_removes_tenant_from_cache(self):
        registry = TenantRegistry()
        row = _make_tenant_row("tid-1")
        registry.put(row)
        assert registry.get("tid-1") is not None

        registry.invalidate("tid-1")
        assert registry.get("tid-1") is None

    def test_no_op_for_unknown_tid(self):
        registry = TenantRegistry()
        registry.invalidate("tid-does-not-exist")

    def test_does_not_affect_other_tenants(self):
        registry = TenantRegistry()
        registry.put(_make_tenant_row("tid-a"))
        registry.put(_make_tenant_row("tid-b"))
        registry.invalidate("tid-a")
        assert registry.get("tid-b") is not None


# ── TenantRegistry.refresh_one ────────────────────────────────────────────────

class TestRefreshOne:
    async def test_returns_cached_tenant_when_found_in_db(self):
        row = _make_tenant_row("tid-1", status="active")
        session = _make_session_filter(row)
        registry = TenantRegistry()
        result = await registry.refresh_one("tid-1", session)
        assert result is not None
        assert result.ms_tenant_id == "tid-1"

    async def test_updates_cache_with_fresh_data(self):
        registry = TenantRegistry()
        old_row = _make_tenant_row("tid-1", status="active")
        registry.put(old_row)
        assert registry.get("tid-1").status == "active"

        fresh_row = _make_tenant_row("tid-1", status="suspended")
        session = _make_session_filter(fresh_row)
        await registry.refresh_one("tid-1", session)
        assert registry.get("tid-1").status == "suspended"

    async def test_returns_none_when_not_in_db(self):
        session = _make_session_filter(None)
        registry = TenantRegistry()
        result = await registry.refresh_one("tid-gone", session)
        assert result is None

    async def test_invalidates_cache_when_not_in_db(self):
        registry = TenantRegistry()
        row = _make_tenant_row("tid-1")
        registry.put(row)
        assert registry.get("tid-1") is not None

        session = _make_session_filter(None)
        await registry.refresh_one("tid-1", session)
        assert registry.get("tid-1") is None
