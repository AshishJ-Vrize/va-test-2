# Scope A — FastAPI app entry point, lifespan, router registration
# Owner: Graph + Routes team
#
# Singleton lifecycle rule:
#   FastAPI path  → lifespan creates objects, stores on app.state
#   Celery path   → app.core.state getters initialise lazily (double-checked lock)

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

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

    kv_client = KeyVaultClient()

    central_engine = _create_central_engine()
    central_session_factory = async_sessionmaker(
        central_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    tenant_registry = TenantRegistry()
    db_manager = DatabaseManager(kv_client)

    redis_client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

    jwks_cache = JWKSCache(redis_client)
    token_verifier = TokenVerifier(jwks_cache)

    # ── Store on app.state ────────────────────────────────────────────────────
    app.state.kv_client = kv_client
    app.state.central_engine = central_engine
    app.state.central_session_factory = central_session_factory
    app.state.tenant_registry = tenant_registry
    app.state.db_manager = db_manager
    app.state.redis = redis_client
    app.state.jwks_cache = jwks_cache
    app.state.token_verifier = token_verifier

    # ── Mirror into state.py so Celery gets the same instances ───────────────
    _state._kv_client = kv_client
    _state._central_engine = central_engine
    _state._central_session_factory = central_session_factory
    _state._tenant_registry = tenant_registry
    _state._db_manager = db_manager
    _state._redis = redis_client
    _state._jwks_cache = jwks_cache
    _state._token_verifier = token_verifier

    log.info("lifespan: startup complete")
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("lifespan: shutdown begin")
    await redis_client.aclose()
    await central_engine.dispose()
    log.info("lifespan: shutdown complete")


app = FastAPI(
    title="Video Analytics Platform API",
    version="0.1.0",
    lifespan=lifespan,
)

from fastapi import APIRouter                                   # noqa: E402
from fastapi.responses import JSONResponse                      # noqa: E402
from app.api.middleware.tenant import RequestTracingMiddleware  # noqa: E402
from app.api.routes.ingest import router as ingest_router      # noqa: E402
from app.api.routes.webhook import router as webhook_router    # noqa: E402
from app.db.central.session import check_central_db_health     # noqa: E402

app.add_middleware(RequestTracingMiddleware)
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
app.include_router(ingest_router)

_health_router = APIRouter()

@_health_router.get("/health", tags=["health"])
async def health_check() -> JSONResponse:
    db_ok = await check_central_db_health()
    if not db_ok:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "db": "unreachable"})
    return JSONResponse(status_code=200, content={"status": "ok"})

app.include_router(_health_router)
