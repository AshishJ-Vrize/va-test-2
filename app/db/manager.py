from __future__ import annotations

import logging
import os
from collections.abc import Generator
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from psycopg2 import OperationalError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.registry import CachedTenant
from app.db.tenant.session import make_tenant_engine, make_tenant_session_factory

if TYPE_CHECKING:
    from app.core.keyvault import KeyVaultClient

log = logging.getLogger(__name__)


@dataclass
class _PoolEntry:
    engine: Engine
    session_factory: sessionmaker[Session]


def _db_name(org_name: str) -> str:
    # OPEN QUESTION — CONTEXT.md §17 #3: naming convention (e.g. pg-{org_name}) not yet
    # confirmed by provisioning team. Change only this function when confirmed.
    return org_name


class DatabaseManager:
    """
    Per-tenant connection pool cache.

    One _PoolEntry per tenant — created on first access, reused forever.
    Key Vault is called once per tenant (at pool creation) and again only
    when PostgreSQL rejects auth (password rotation in Key Vault).

    Thread safety: double-check locking in _get_or_create_factory().
    The outer check (no lock) avoids acquiring the lock on the hot path.
    The inner check (under lock) prevents two threads from both seeing a
    cache miss and both creating a pool for the same tenant.
    """

    def __init__(self, kv_client: KeyVaultClient) -> None:
        self._kv = kv_client
        self._pool_cache: dict[str, _PoolEntry] = {}
        self._lock = Lock()

    def get_session(
        self, tid: str, cached_tenant: CachedTenant
    ) -> Generator[Session, None, None]:
        entry = self._get_or_create_factory(cached_tenant)
        session = entry.session_factory()
        try:
            yield session
            session.commit()
        except OperationalError as exc:
            session.rollback()
            # pgcode 28P01 = invalid_password; also check message for robustness
            pgcode = getattr(exc.orig, "pgcode", None)
            is_auth_error = pgcode == "28P01" or (
                exc.orig is not None
                and "password authentication failed" in str(exc.orig).lower()
            )
            if is_auth_error:
                log.warning(
                    "Auth error for tenant %s — evicting pool entry and retrying with "
                    "fresh Key Vault secret.",
                    tid,
                )
                with self._lock:
                    self._pool_cache.pop(tid, None)
                entry = self._get_or_create_factory(cached_tenant)
                session = entry.session_factory()
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()
            else:
                raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _get_or_create_factory(self, cached_tenant: CachedTenant) -> _PoolEntry:
        tid = cached_tenant.ms_tenant_id
        # Outer check — no lock, avoids contention on hot path
        if tid in self._pool_cache:
            return self._pool_cache[tid]

        with self._lock:
            # Inner check — another thread may have created the entry while we waited
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
        db_user = os.environ["TENANT_DB_USER"]
        db_host = cached_tenant.db_host
        db_name = _db_name(cached_tenant.org_name)
        return (
            f"postgresql+psycopg2://{db_user}:{secret}"
            f"@{db_host}/{db_name}?sslmode=require"
        )

# No module-level singleton — use app.core.state.get_db_manager()
# FastAPI deps read from app.state (set by lifespan). Celery uses state.py directly.
