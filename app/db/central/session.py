from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings

log = logging.getLogger(__name__)

_APP_ENV = os.getenv("APP_ENV", "development")


def _create_central_engine() -> Engine:
    """
    Builds the SQLAlchemy engine for the central DB.
    Called by lifespan (main.py) on startup and by state.py for lazy Celery init.
    Not called at import time — no module-level side effects.
    """
    central_db_url = get_settings().CENTRAL_DB_URL
    if "sslmode=require" not in central_db_url:
        if _APP_ENV == "production":
            raise ValueError(
                "CENTRAL_DB_URL must include sslmode=require in production. "
                "Current value is missing it."
            )
        log.warning(
            "CENTRAL_DB_URL does not include sslmode=require. "
            "Acceptable in development; required in production."
        )

    return create_engine(
        central_db_url,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_timeout=30,
    )


def get_central_db(request) -> Generator[Session, None, None]:
    """
    FastAPI dependency — yields a central DB session.
    Reads the session factory from app.state (set by lifespan in main.py).
    FastAPI injects `request` automatically — callers use Depends(get_central_db).
    """
    session: Session = request.app.state.central_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def central_session() -> Generator[Session, None, None]:
    """
    Context manager for Celery tasks and scripts (no FastAPI Request available).
    Uses state.py lazy getter so it works whether or not lifespan has run.
    """
    from app.core import state
    session: Session = state.get_central_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_central_db_health() -> bool:
    """Used by /health route. Returns True if the DB is reachable."""
    try:
        from app.core import state
        with state.get_central_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        log.exception("Central DB health check failed")
        return False
