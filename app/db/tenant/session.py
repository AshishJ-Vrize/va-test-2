from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


def make_tenant_engine(url: str) -> AsyncEngine:
    """
    Creates a per-tenant async SQLAlchemy engine.
    URL must use the psycopg3 async driver: postgresql+psycopg_async://
    pool_size=2, max_overflow=3: conservative per CONTEXT.md §10.
    """
    if "sslmode=require" not in url:
        raise ValueError(
            "Tenant DB URL must include sslmode=require. "
            f"Received URL is missing it: {url!r}"
        )

    return create_async_engine(
        url,
        pool_size=2,
        max_overflow=3,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_timeout=30,
    )


def make_tenant_session_factory(engine: AsyncEngine) -> sessionmaker[AsyncSession]:
    return sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
