# Singleton provider — dual-path pattern
#
# FastAPI path:  lifespan (main.py) creates all objects, writes them here
#                AND to app.state. FastAPI deps read from app.state via Request.
#
# Celery path:   tasks import get_X() from this module. On first call, each
#                getter creates the singleton lazily (double-checked locking).
#                If FastAPI lifespan already ran, the module-level var is already
#                set — no duplicate creation.
#
# Rule for new singletons: add a module-level var + getter here, then
# initialise it in lifespan (main.py) and add it to app.state.

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as redis_lib
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.core.keyvault import KeyVaultClient
    from app.core.security import JWKSCache, TokenVerifier
    from app.db.manager import DatabaseManager
    from app.db.registry import TenantRegistry

log = logging.getLogger(__name__)

_lock = threading.Lock()

# ── Module-level singleton slots ──────────────────────────────────────────────

_kv_client: KeyVaultClient | None = None
_db_manager: DatabaseManager | None = None
_tenant_registry: TenantRegistry | None = None
_central_engine: AsyncEngine | None = None
_central_session_factory: async_sessionmaker[AsyncSession] | None = None
_redis: redis_lib.Redis | None = None
_jwks_cache: JWKSCache | None = None
_token_verifier: TokenVerifier | None = None


# ── Getters (lazy init with double-checked locking) ───────────────────────────

def get_kv_client() -> KeyVaultClient:
    global _kv_client
    if _kv_client is None:
        with _lock:
            if _kv_client is None:
                from app.core.keyvault import KeyVaultClient
                log.info("state: lazy-initialising KeyVaultClient")
                _kv_client = KeyVaultClient()
    return _kv_client


def get_db_manager() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        with _lock:
            if _db_manager is None:
                from app.db.manager import DatabaseManager
                log.info("state: lazy-initialising DatabaseManager")
                _db_manager = DatabaseManager(get_kv_client())
    return _db_manager


def get_tenant_registry() -> TenantRegistry:
    global _tenant_registry
    if _tenant_registry is None:
        with _lock:
            if _tenant_registry is None:
                from app.db.registry import TenantRegistry
                log.info("state: lazy-initialising TenantRegistry")
                _tenant_registry = TenantRegistry()
    return _tenant_registry


def get_central_engine() -> AsyncEngine:
    global _central_engine
    if _central_engine is None:
        with _lock:
            if _central_engine is None:
                from app.db.central.session import _create_central_engine
                log.info("state: lazy-initialising central DB engine")
                _central_engine = _create_central_engine()
    return _central_engine


def get_central_session_factory() -> async_sessionmaker[AsyncSession]:
    global _central_session_factory
    if _central_session_factory is None:
        with _lock:
            if _central_session_factory is None:
                from sqlalchemy.ext.asyncio import async_sessionmaker
                log.info("state: lazy-initialising central session factory")
                _central_session_factory = async_sessionmaker(
                    get_central_engine(),
                    autoflush=False,
                    autocommit=False,
                    expire_on_commit=False,
                )
    return _central_session_factory


def get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        with _lock:
            if _redis is None:
                import redis.asyncio as aioredis
                from app.config.settings import get_settings
                log.info("state: lazy-initialising async Redis client")
                _redis = aioredis.Redis.from_url(
                    get_settings().REDIS_URL, decode_responses=True
                )
    return _redis


def get_jwks_cache() -> JWKSCache:
    global _jwks_cache
    if _jwks_cache is None:
        with _lock:
            if _jwks_cache is None:
                from app.core.security import JWKSCache
                log.info("state: lazy-initialising JWKSCache")
                _jwks_cache = JWKSCache(get_redis())
    return _jwks_cache


def get_token_verifier() -> TokenVerifier:
    global _token_verifier
    if _token_verifier is None:
        with _lock:
            if _token_verifier is None:
                from app.core.security import TokenVerifier
                log.info("state: lazy-initialising TokenVerifier")
                _token_verifier = TokenVerifier(get_jwks_cache())
    return _token_verifier
