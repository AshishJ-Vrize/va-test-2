# Scope A — FastAPI app entry point, lifespan, router registration
# Owner: Graph + Routes team
#
# Singleton lifecycle rule:
#   FastAPI path  → lifespan creates objects, stores on app.state
#   Celery path   → app.core.state getters initialise lazily (double-checked lock)

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import ssl
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

    # redis-py 6.x requires ssl_cert_reqs as a Python ssl constant, not as a URL param.
    # Celery parses CERT_NONE from the URL itself, so we strip it here and pass directly.
    _redis_url = settings.REDIS_URL
    _ssl_kwargs: dict = {}
    if "ssl_cert_reqs" in _redis_url:
        _parsed = urlparse(_redis_url)
        _params = parse_qs(_parsed.query)
        _params.pop("ssl_cert_reqs", None)
        _redis_url = urlunparse(_parsed._replace(query=urlencode({k: v[0] for k, v in _params.items()})))
        _ssl_kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
    redis_client = aioredis.Redis.from_url(_redis_url, decode_responses=True, **_ssl_kwargs)

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

from app.api.middleware.tenant import RequestTracingMiddleware  # noqa: E402
from app.api.routes.auth import router as auth_router          # noqa: E402
from app.api.routes.chat import router as chat_router          # noqa: E402
from app.api.routes.health import router as health_router      # noqa: E402
from app.api.routes.ingest import router as ingest_router      # noqa: E402
from app.api.routes.meetings import router as meetings_router  # noqa: E402
from app.api.routes.seed import router as seed_router          # noqa: E402
from app.api.routes.webhook import router as webhook_router    # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",   # Next.js dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestTracingMiddleware)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(seed_router)
app.include_router(webhook_router, prefix="/api/v1/webhook", tags=["webhook"])
app.include_router(ingest_router)
app.include_router(meetings_router)
