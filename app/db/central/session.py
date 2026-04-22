from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config.settings import get_settings

log = logging.getLogger(__name__)

_APP_ENV = os.getenv("APP_ENV", "development")


def _create_central_engine() -> AsyncEngine:
    """
    Builds the async SQLAlchemy engine for the central DB.
    Called by lifespan (main.py) on startup and by state.py for lazy Celery init.
    Not called at import time — no module-level side effects.
    URL must use psycopg3 async driver: postgresql+psycopg_async://
    """
    central_db_url = get_settings().CENTRAL_DB_URL
    if "sslmode=require" not in central_db_url:
        if _APP_ENV == "production":
            raise ValueError(
                "CENTRAL_DB_URL must include sslmode=require in production."
            )
        log.warning(
            "CENTRAL_DB_URL does not include sslmode=require. "
            "Acceptable in development; required in production."
        )

    return create_async_engine(
        central_db_url,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_timeout=30,
    )


async def get_central_db(request) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields a central DB async session.
    Reads the session factory from app.state (set by lifespan in main.py).
    FastAPI injects `request` automatically — callers use Depends(get_central_db).
    """
    session: AsyncSession = request.app.state.central_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def central_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for Celery tasks and scripts (no FastAPI Request).
    Uses state.py lazy getter so it works whether or not lifespan has run.
    """
    from app.core import state
    session: AsyncSession = state.get_central_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_central_db_health() -> bool:
    """Used by /health route. Returns True if the DB is reachable."""
    try:
        from app.core import state
        async with state.get_central_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        log.exception("Central DB health check failed")
        return False
