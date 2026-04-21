"""
Run Alembic tenant migrations across all active tenant DBs in parallel.

Usage:
    python scripts/migrate_all_tenants.py
    python scripts/migrate_all_tenants.py --tenant acme          # single tenant
    python scripts/migrate_all_tenants.py --workers 20           # parallelism
    python scripts/migrate_all_tenants.py --sql                  # dry-run SQL only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config

# Resolve project root before any app imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.central.session import central_session  # noqa: E402
from app.db.central.models import Tenant  # noqa: E402
from app.core.keyvault import key_vault_client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

_TENANT_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic" / "tenant" / "alembic.ini"

# deprovisioned tenants have no live DB — skip them
_MIGRATABLE_STATUSES = {"provisioning", "active", "suspended"}


@dataclass
class MigrationResult:
    org_name: str
    success: bool
    error: str = ""


@dataclass
class MigrationSummary:
    results: list[MigrationResult] = field(default_factory=list)

    @property
    def failed(self) -> list[MigrationResult]:
        return [r for r in self.results if not r.success]

    @property
    def succeeded(self) -> list[MigrationResult]:
        return [r for r in self.results if r.success]


def _db_name(org_name: str) -> str:
    # OPEN QUESTION — CONTEXT.md §17 #3: mirrors manager.py; change both when confirmed.
    return org_name


def _build_url(tenant: Tenant) -> str:
    secret = key_vault_client.get_db_secret(tenant.org_name)
    db_user = os.environ["TENANT_DB_USER"]
    db_name = _db_name(tenant.org_name)
    return (
        f"postgresql+psycopg2://{db_user}:{secret}"
        f"@{tenant.db_host}/{db_name}?sslmode=require"
    )


def _migrate_tenant(tenant: Tenant, sql_only: bool) -> MigrationResult:
    org_name = tenant.org_name
    try:
        alembic_cfg = Config(str(_TENANT_ALEMBIC_INI))
        alembic_cfg.set_main_option("sqlalchemy.url", _build_url(tenant))

        if sql_only:
            command.upgrade(alembic_cfg, "head", sql=True)
        else:
            command.upgrade(alembic_cfg, "head")

        log.info("[OK] %s", org_name)
        return MigrationResult(org_name=org_name, success=True)
    except Exception as exc:
        log.error("[FAIL] %s: %s", org_name, exc)
        return MigrationResult(org_name=org_name, success=False, error=str(exc))


@dataclass
class _TenantSnapshot:
    """Plain data object — avoids DetachedInstanceError after session closes."""
    org_name: str
    db_host: str
    status: str


def _load_tenants(org_name_filter: str | None) -> list[_TenantSnapshot]:
    with central_session() as session:
        q = session.query(Tenant).filter(Tenant.status.in_(_MIGRATABLE_STATUSES))
        if org_name_filter:
            q = q.filter(Tenant.org_name == org_name_filter)
        rows = q.all()
        # Touch all attributes inside the session to prevent DetachedInstanceError
        return [
            _TenantSnapshot(
                org_name=row.org_name,
                db_host=row.db_host,
                status=row.status,
            )
            for row in rows
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate all tenant DBs to head")
    parser.add_argument("--tenant", metavar="ORG_NAME", help="Migrate a single tenant only")
    parser.add_argument("--workers", type=int, default=10, help="ThreadPoolExecutor workers (default: 10)")
    parser.add_argument("--sql", action="store_true", help="Dry-run: print SQL instead of executing")
    args = parser.parse_args()

    snapshots = _load_tenants(args.tenant)
    if not snapshots:
        log.warning("No migratable tenants found (filter=%s)", args.tenant)
        sys.exit(0)

    log.info(
        "Migrating %d tenant(s) with %d worker(s) (sql=%s)",
        len(snapshots),
        min(args.workers, len(snapshots)),
        args.sql,
    )

    # Re-query as Tenant objects inside the executor — each thread needs its own
    # session. We pass org_name strings so threads can fetch their own Tenant row.
    summary = MigrationSummary()
    with ThreadPoolExecutor(max_workers=min(args.workers, len(snapshots))) as executor:
        futures = {
            executor.submit(_migrate_by_org_name, snap.org_name, args.sql): snap.org_name
            for snap in snapshots
        }
        for future in as_completed(futures):
            summary.results.append(future.result())

    log.info(
        "Migration complete — %d succeeded, %d failed",
        len(summary.succeeded),
        len(summary.failed),
    )
    for result in summary.failed:
        log.error("  FAILED: %s — %s", result.org_name, result.error)

    if summary.failed:
        sys.exit(1)


def _migrate_by_org_name(org_name: str, sql_only: bool) -> MigrationResult:
    """Each worker thread fetches its own Tenant row in its own session."""
    with central_session() as session:
        tenant = session.query(Tenant).filter(Tenant.org_name == org_name).first()
        if tenant is None:
            return MigrationResult(org_name=org_name, success=False, error="Tenant not found")
        # Build all data while session is open
        try:
            url = _build_url(tenant)
        except Exception as exc:
            return MigrationResult(org_name=org_name, success=False, error=f"Key Vault error: {exc}")

    try:
        alembic_cfg = Config(str(_TENANT_ALEMBIC_INI))
        alembic_cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(alembic_cfg, "head", sql=sql_only)
        log.info("[OK] %s", org_name)
        return MigrationResult(org_name=org_name, success=True)
    except Exception as exc:
        log.error("[FAIL] %s: %s", org_name, exc)
        return MigrationResult(org_name=org_name, success=False, error=str(exc))


if __name__ == "__main__":
    main()
