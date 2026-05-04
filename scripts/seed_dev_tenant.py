"""Insert / update the dev tenant row in va_central_dev.

Routes any Azure AD user from your AZURE_TENANT_ID to the va_alpha_v2 tenant DB.

Reads from .env:
    CENTRAL_DB_URL  - must point at va_central_dev (any sync/async driver scheme)
    AZURE_TENANT_ID - your Azure AD tenant; goes into tenants.ms_tenant_id

Derives:
    org_name        = 'alpha_v2'  (manager.py turns this into the DB name 'va_alpha_v2')
    db_host         = host parsed from CENTRAL_DB_URL
    blob_container  = 'va-alpha_v2'

Idempotent — safe to re-run; updates the existing row instead of inserting a duplicate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert

EXPECTED_CENTRAL_DB = "va_central_dev"
ORG_NAME = "alpha_v2"
DISPLAY_NAME = "Alpha v2 (RAG demo)"

_ASYNC_TO_SYNC = {
    "postgresql+psycopg_async": "postgresql+psycopg",
    "postgresql+asyncpg":       "postgresql+psycopg",
}


def _to_sync_url(url: str) -> str:
    for async_scheme, sync_scheme in _ASYNC_TO_SYNC.items():
        if url.startswith(async_scheme + "://"):
            return sync_scheme + url[len(async_scheme):]
    return url


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"
    if not env_path.exists():
        raise SystemExit(f"REFUSING: {env_path} does not exist.")
    load_dotenv(env_path, override=True)
    print(f"loaded {env_path}")

    raw_url = os.environ.get("CENTRAL_DB_URL")
    if not raw_url:
        raise SystemExit("CENTRAL_DB_URL not set in .env")
    ms_tenant_id = os.environ.get("AZURE_TENANT_ID")
    if not ms_tenant_id:
        raise SystemExit("AZURE_TENANT_ID not set in .env")

    central_url = _to_sync_url(raw_url)
    parsed = urlparse(central_url)

    db_in_url = parsed.path.lstrip("/")
    if db_in_url != EXPECTED_CENTRAL_DB:
        raise SystemExit(
            f"CENTRAL_DB_URL must point at '{EXPECTED_CENTRAL_DB}', got '{db_in_url}'."
        )
    db_host = parsed.hostname
    if not db_host:
        raise SystemExit("Could not parse host from CENTRAL_DB_URL")

    from app.db.central.models import Tenant

    engine = create_engine(central_url)

    # Belt-and-braces safety check.
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
        if actual != EXPECTED_CENTRAL_DB:
            raise SystemExit(
                f"REFUSING: connected to '{actual}', expected '{EXPECTED_CENTRAL_DB}'."
            )

    values = dict(
        org_name=ORG_NAME,
        display_name=DISPLAY_NAME,
        ms_tenant_id=ms_tenant_id,
        db_host=db_host,
        db_region="local-dev",
        db_sku="local",
        blob_container=f"va-{ORG_NAME}",
        status="active",
        plan="enterprise",
        max_users=100,
    )

    stmt = insert(Tenant).values(**values).on_conflict_do_update(
        index_elements=["org_name"],
        set_={k: v for k, v in values.items() if k != "org_name"},
    )

    with engine.begin() as conn:
        conn.execute(stmt)

    print(f"✓ tenant row upserted in {EXPECTED_CENTRAL_DB}.tenants:")
    print(f"    org_name      = {ORG_NAME}")
    print(f"    display_name  = {DISPLAY_NAME}")
    print(f"    ms_tenant_id  = {ms_tenant_id}")
    print(f"    db_host       = {db_host}")
    print(f"    status        = active")
    print()
    print(f"  Sign-ins from Azure AD tenant {ms_tenant_id} will now route to va_alpha_v2.")


if __name__ == "__main__":
    main()
