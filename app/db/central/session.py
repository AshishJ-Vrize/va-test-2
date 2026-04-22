from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import settings

log = logging.getLogger(__name__)

_APP_ENV = os.getenv("APP_ENV", "development")


def _create_central_engine() -> Engine:
    if "sslmode=require" not in settings.CENTRAL_DB_URL:
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
        settings.CENTRAL_DB_URL,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_timeout=30,
    )


engine: Engine = _create_central_engine()

_session_factory: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_central_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a session, rolls back on error, always closes."""
    session = _session_factory()
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
    """Context manager for scripts and Celery tasks that don't use FastAPI Depends."""
    session = _session_factory()
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
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        log.exception("Central DB health check failed")
        return False
