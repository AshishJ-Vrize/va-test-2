from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote_plus
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, AsyncGenerator

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.db.registry import CachedTenant
from app.db.tenant.session import make_tenant_engine, make_tenant_session_factory

if TYPE_CHECKING:
    from app.core.keyvault import KeyVaultClient

log = logging.getLogger(__name__)


@dataclass
class _PoolEntry:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def _db_name(org_name: str) -> str:
    # DB naming convention: va_{org_name} (e.g. va_vrize for org_name="vrize")
    return f"va_{org_name}"


class DatabaseManager:
    """
    Per-tenant async connection pool cache.

    One _PoolEntry per tenant — created on first access, reused forever.
    Key Vault is called once per tenant (at pool creation) and again only
    when PostgreSQL rejects auth (password rotation in Key Vault).

    Thread safety: double-check locking in _get_or_create_factory().
    _get_or_create_factory() is sync and run via asyncio.to_thread() so the
    blocking Key Vault call never blocks the async event loop.
    """

    def __init__(self, kv_client: KeyVaultClient) -> None:
        self._kv = kv_client
        self._pool_cache: dict[str, _PoolEntry] = {}
        self._lock = Lock()

    @asynccontextmanager
    async def get_session(
        self, tid: str, cached_tenant: CachedTenant
    ) -> AsyncGenerator[AsyncSession, None]:
        """
        Async context manager — yields a tenant-scoped AsyncSession.
        Commits on clean exit, rolls back on exception.

        On PostgreSQL auth error (28P01 — password rotated in Key Vault):
        evicts the pool entry and raises. The next request will re-create
        the pool with a fresh secret from Key Vault.
        """
        entry = await asyncio.to_thread(self._get_or_create_factory, cached_tenant)
        session: AsyncSession = entry.session_factory()
        try:
            yield session
            await session.commit()
        except OperationalError as exc:
            await session.rollback()
            pgcode = getattr(exc.orig, "pgcode", None)
            is_auth_error = pgcode == "28P01" or (
                "password authentication failed" in str(exc).lower()
            )
            if is_auth_error:
                log.warning(
                    "Auth error for tenant %s — evicting pool entry. "
                    "Next request will use fresh Key Vault secret.", tid,
                )
                with self._lock:
                    self._pool_cache.pop(tid, None)
            raise
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    def _get_or_create_factory(self, cached_tenant: CachedTenant) -> _PoolEntry:
        """
        Sync — safe to call from asyncio.to_thread().
        Double-checked locking prevents duplicate pool creation under concurrency.
        """
        tid = cached_tenant.ms_tenant_id
        if tid in self._pool_cache:
            return self._pool_cache[tid]

        with self._lock:
            if tid in self._pool_cache:
                return self._pool_cache[tid]

            url = self._build_url(cached_tenant)
            engine = make_tenant_engine(url)
            factory = make_tenant_session_factory(engine)
            entry = _PoolEntry(engine=engine, session_factory=factory)
            self._pool_cache[tid] = entry
            log.info("Created DB pool for tenant %s (%s)", cached_tenant.org_name, tid)
            return entry

    def _build_url(self, cached_tenant: CachedTenant) -> str:
        secret = self._kv.get_db_secret(cached_tenant.org_name)
        db_user = get_settings().TENANT_DB_USER
        db_host = cached_tenant.db_host
        db_name = _db_name(cached_tenant.org_name)
        # psycopg3 async driver — postgresql+psycopg_async://
        return (
            f"postgresql+psycopg_async://{quote_plus(db_user)}:{quote_plus(secret)}"
            f"@{db_host}/{db_name}?sslmode=require"
        )

# No module-level singleton — use app.core.state.get_db_manager()
# FastAPI deps read from app.state (set by lifespan). Celery uses state.py directly.
