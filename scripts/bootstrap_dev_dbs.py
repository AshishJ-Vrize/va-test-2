"""One-off: bootstrap the local dev central + tenant databases.

Materialises the SQLAlchemy schema in TWO specific databases:
    * va_central_dev   (central — tenants, billing, etc.)
    * va_alpha_v2      (tenant  — meetings, chunks, chat, etc.)

Connection details are read from `.env` in the project root:
    * CENTRAL_DB_URL  must point at va_central_dev (any sync/async driver scheme)

The tenant URL is derived automatically by swapping the database name —
same host, same user, same password — so you only need one URL in .env.

The script verifies it is connected to those exact database names before
doing anything, so it cannot accidentally modify any other database on
the same Postgres server (e.g. prod's va_central or va_vrize).

Usage
-----
    # Set CENTRAL_DB_URL in .env, then:
    python scripts/bootstrap_dev_dbs.py

The tenant DB must have the `vector` extension enabled before running this.
The script will refuse and tell you the exact SQL to run if it isn't.

Note: this script does NOT insert a row into va_central_dev.tenants. That
is a follow-up SQL step with values you choose (ms_tenant_id, db_host, etc.).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from alembic import command
from alembic.config import Config

EXPECTED_CENTRAL_DB = "va_central_dev"
EXPECTED_TENANT_DB = "va_alpha_v2"

# Map any async / non-sync driver scheme to a sync psycopg3 scheme so that
# create_engine() works without an event loop. .env may legitimately use the
# async driver for the running app — this conversion is bootstrap-only.
_ASYNC_TO_SYNC = {
    "postgresql+psycopg_async": "postgresql+psycopg",
    "postgresql+asyncpg":       "postgresql+psycopg",
}


def _to_sync_url(url: str) -> str:
    """Force the URL onto a sync driver scheme so create_engine() can use it."""
    for async_scheme, sync_scheme in _ASYNC_TO_SYNC.items():
        if url.startswith(async_scheme + "://"):
            return sync_scheme + url[len(async_scheme):]
    return url


def _swap_db_name(url: str, new_name: str) -> str:
    """Replace the database segment of a Postgres URL with `new_name`."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{new_name}"))


def _assert_db_name(engine, expected: str) -> None:
    """Refuse to operate against any database other than the expected one."""
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
    if actual != expected:
        raise SystemExit(
            f"REFUSING: connected database is '{actual}', expected '{expected}'.\n"
            f"Aborting to prevent accidental modification of another database."
        )


def bootstrap_central(url: str) -> None:
    """Create the central schema in va_central_dev."""
    from app.db.central.models import Base as CentralBase

    engine = create_engine(url)
    _assert_db_name(engine, EXPECTED_CENTRAL_DB)
    CentralBase.metadata.create_all(engine)
    print(f"✓ central schema materialised in {EXPECTED_CENTRAL_DB}")
    print("  tables: tenants, credit_pricing, billing_periods, invoices")


def bootstrap_tenant(url: str) -> None:
    """Create the tenant schema in va_alpha_v2 and stamp alembic to head."""
    from app.db.tenant.models import Base as TenantBase

    engine = create_engine(url)
    _assert_db_name(engine, EXPECTED_TENANT_DB)

    # Pre-flight: pgvector extension is required by the chunks.embedding column.
    with engine.connect() as conn:
        has_vector = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    if not has_vector:
        raise SystemExit(
            f"REFUSING: 'vector' extension not enabled in {EXPECTED_TENANT_DB}.\n"
            f"Connect to that database and run:\n"
            f"    CREATE EXTENSION IF NOT EXISTS vector;"
        )

    TenantBase.metadata.create_all(engine)
    print(f"✓ tenant schema materialised in {EXPECTED_TENANT_DB}")

    # Stamp alembic so future migrations apply on top of this v2 baseline,
    # rather than trying to re-run all the historical ALTER statements.
    cfg = Config("alembic/tenant/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.stamp(cfg, "head")
    print("✓ alembic stamped to head (20260504_0001)")


def main() -> None:
    # Load .env from the project root, and override any pre-existing env vars
    # so the file is the source of truth for this run.
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    if not env_path.exists():
        raise SystemExit(f"REFUSING: {env_path} does not exist.")
    load_dotenv(env_path, override=True)
    print(f"loaded {env_path}")

    raw_central_url = os.environ.get("CENTRAL_DB_URL")
    if not raw_central_url:
        raise SystemExit(
            "CENTRAL_DB_URL is not set in .env. Add a line like:\n"
            "  CENTRAL_DB_URL=postgresql+psycopg://va_admin:PWD@your-host:5432/va_central_dev?sslmode=require"
        )

    # Force sync driver for create_engine + sanity-check the DB name in the URL.
    central_url = _to_sync_url(raw_central_url)
    parsed = urlparse(central_url)
    db_in_url = parsed.path.lstrip("/")
    if db_in_url != EXPECTED_CENTRAL_DB:
        raise SystemExit(
            f"CENTRAL_DB_URL must point at '{EXPECTED_CENTRAL_DB}', "
            f"but the URL's database is '{db_in_url}'.\n"
            f"Update .env so the URL ends with /{EXPECTED_CENTRAL_DB}?sslmode=require"
        )

    # Tenant URL = same host/user/pwd, just swap the database name.
    tenant_url = _swap_db_name(central_url, EXPECTED_TENANT_DB)

    bootstrap_central(central_url)
    print()
    bootstrap_tenant(tenant_url)
    print()
    print("Done. Only va_central_dev and va_alpha_v2 were modified.")


if __name__ == "__main__":
    main()
