from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_tenant_engine(url: str) -> Engine:
    """
    Validates that the URL enforces SSL then creates a per-tenant engine.
    pool_size=2, max_overflow=3: conservative per CONTEXT.md §10 — 500 tenants
    sharing the same process means we cannot afford large pools per tenant.
    """
    if "sslmode=require" not in url:
        raise ValueError(
            "Tenant DB URL must include sslmode=require. "
            f"Received URL is missing it: {url!r}"
        )

    return create_engine(
        url,
        pool_size=2,
        max_overflow=3,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_timeout=30,
    )


def make_tenant_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
