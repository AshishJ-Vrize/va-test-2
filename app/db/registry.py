from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import RLock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.central.models import Tenant

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedTenant:
    """
    Immutable snapshot of a tenants row.
    frozen=True means attribute mutation raises FrozenInstanceError at runtime —
    threads share this object safely without any locking on reads.
    """

    id: str
    org_name: str
    ms_tenant_id: str
    db_host: str
    db_region: str
    status: str
    plan: str
    max_users: int


class TenantRegistry:
    """
    In-memory cache keyed by ms_tenant_id (the JWT `tid` claim).

    Design choices:
    - RLock (reentrant) instead of Lock so the same thread can call
      refresh_one() from within a block that already holds the lock.
    - load_all() replaces the whole cache atomically under the lock.
    - get() is intentionally lock-free on the read path — dict reads
      in CPython are safe due to the GIL, and CachedTenant is immutable.
    - load_all() and refresh_one() are async — they use AsyncSession.
    """

    def __init__(self) -> None:
        self._cache: dict[str, CachedTenant] = {}
        self._lock = RLock()

    async def load_all(self, session: AsyncSession) -> None:
        result = await session.execute(select(Tenant))
        rows = result.scalars().all()
        new_cache: dict[str, CachedTenant] = {}
        for row in rows:
            cached = CachedTenant(
                id=str(row.id),
                org_name=row.org_name,
                ms_tenant_id=row.ms_tenant_id,
                db_host=row.db_host,
                db_region=row.db_region,
                status=row.status,
                plan=row.plan,
                max_users=row.max_users,
            )
            new_cache[row.ms_tenant_id] = cached

        with self._lock:
            self._cache = new_cache

        log.info("TenantRegistry loaded %d tenants", len(new_cache))

    def get(self, ms_tenant_id: str) -> CachedTenant | None:
        return self._cache.get(ms_tenant_id)

    def put(self, tenant: Tenant) -> None:
        cached = CachedTenant(
            id=str(tenant.id),
            org_name=tenant.org_name,
            ms_tenant_id=tenant.ms_tenant_id,
            db_host=tenant.db_host,
            db_region=tenant.db_region,
            status=tenant.status,
            plan=tenant.plan,
            max_users=tenant.max_users,
        )
        with self._lock:
            self._cache[tenant.ms_tenant_id] = cached

    def invalidate(self, ms_tenant_id: str) -> None:
        with self._lock:
            self._cache.pop(ms_tenant_id, None)

    async def refresh_one(
        self, ms_tenant_id: str, session: AsyncSession
    ) -> CachedTenant | None:
        result = await session.execute(
            select(Tenant).where(Tenant.ms_tenant_id == ms_tenant_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            self.invalidate(ms_tenant_id)
            return None
        self.put(row)
        return self.get(ms_tenant_id)


# No module-level singleton — use app.core.state.get_tenant_registry()
# FastAPI deps read from app.state (set by lifespan). Celery uses state.py directly.
