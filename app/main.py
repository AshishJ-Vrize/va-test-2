# Scope A — FastAPI app entry point, lifespan, router registration
# Owner: Graph + Routes team
#
# Singleton lifecycle rule:
#   FastAPI path  → lifespan creates objects, stores on app.state
#   Celery path   → app.core.state getters initialise lazily (double-checked lock)
#
# Dependency order in lifespan (each step may depend on the previous):
#   1. Settings (already @lru_cache in config/settings.py)
#   2. KeyVaultClient        — no dependencies
#   3. Central DB engine     — needs settings.CENTRAL_DB_URL
#   4. TenantRegistry        — no dependencies
#   5. DatabaseManager       — needs KeyVaultClient
#   6. Redis                 — needs settings.REDIS_URL
#   7. JWKSCache             — needs Redis
#   8. TokenVerifier         — needs JWKSCache

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis as redis_lib
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

import app.core.state as _state
from app.config.settings import get_settings
from app.core.keyvault import KeyVaultClient
from app.core.security import JWKSCache, TokenVerifier
from app.db.central.session import _create_central_engine
from app.db.manager import DatabaseManager
from app.db.registry import TenantRegistry

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    log.info("lifespan: startup begin")
    settings = get_settings()

    # 1. Key Vault
    kv_client = KeyVaultClient()

    # 2. Central DB engine + session factory
    central_engine = _create_central_engine()
    central_session_factory = sessionmaker(
        bind=central_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    # 3. Tenant Registry (no dependencies)
    tenant_registry = TenantRegistry()

    # 4. Database Manager (depends on KeyVaultClient)
    db_manager = DatabaseManager(kv_client)

    # 5. Redis
    redis_client = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)

    # 6. JWKS Cache + Token Verifier
    jwks_cache = JWKSCache(redis_client)
    token_verifier = TokenVerifier(jwks_cache)

    # ── Store on app.state (FastAPI deps read from here via Request) ──────────
    app.state.kv_client = kv_client
    app.state.central_engine = central_engine
    app.state.central_session_factory = central_session_factory
    app.state.tenant_registry = tenant_registry
    app.state.db_manager = db_manager
    app.state.redis = redis_client
    app.state.jwks_cache = jwks_cache
    app.state.token_verifier = token_verifier

    # ── Mirror into state.py so Celery gets the same instances, not new ones ──
    # If a Celery task calls state.get_db_manager() after this point, it gets
    # the exact same object that FastAPI deps use — no duplicate connections.
    _state._kv_client = kv_client
    _state._central_engine = central_engine
    _state._central_session_factory = central_session_factory
    _state._tenant_registry = tenant_registry
    _state._db_manager = db_manager
    _state._redis = redis_client
    _state._jwks_cache = jwks_cache
    _state._token_verifier = token_verifier

    log.info("lifespan: startup complete")

    yield  # ── App is running ─────────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("lifespan: shutdown begin")
    redis_client.close()
    central_engine.dispose()  # closes all pooled central DB connections
    log.info("lifespan: shutdown complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Video Analytics Platform API",
    version="0.1.0",
    lifespan=lifespan,
    # Disable automatic /docs and /redoc in production if needed via settings
)


# ── Routers ───────────────────────────────────────────────────────────────────
# Register routers here as they are implemented.
# Pattern: from app.api.routes.X import router as X_router
#          app.include_router(X_router, prefix="/X", tags=["X"])

from app.api.middleware.tenant import RequestTracingMiddleware  # noqa: E402
from app.api.routes.webhook import router as webhook_router    # noqa: E402

app.add_middleware(RequestTracingMiddleware)
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
