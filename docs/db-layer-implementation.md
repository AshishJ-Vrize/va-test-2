# DB Layer Implementation — Video Analytics Platform

**Branch:** `feature/db-models`  
**Commit:** `c37705a`  
**Author:** Ashish Jaiswal  
**Date:** 22 April 2026  
**Status:** Pushed — pending PR to `main`

---

## Overview

This document covers the complete implementation of the database layer for the Video Analytics multi-tenant SaaS platform. The work covers SQLAlchemy models, session factories, tenant registry, database manager, Alembic migration environments, and a parallel tenant migration script.

No files outside `app/db/`, `alembic/`, and `scripts/` were modified.

---

## Files Changed (9 files, 1367 insertions)

| File | What it does |
|------|-------------|
| `app/db/central/models.py` | SQLAlchemy models for the shared central DB |
| `app/db/tenant/models.py` | SQLAlchemy models for all 15 per-tenant tables |
| `app/db/central/session.py` | Central DB engine, session factory, health check |
| `app/db/tenant/session.py` | Per-tenant session factory functions |
| `app/db/registry.py` | In-memory tenant config cache (TenantRegistry) |
| `app/db/manager.py` | Per-tenant connection pool manager (DatabaseManager) |
| `alembic/central/env.py` | Alembic migration environment for central DB |
| `alembic/tenant/env.py` | Alembic migration environment for tenant DBs |
| `scripts/migrate_all_tenants.py` | Script to run tenant migrations in parallel |

---

## Multi-Tenancy Architecture

**Strategy:** DB-per-tenant isolation. Each client has an isolated Azure PostgreSQL Flexible Server instance. One shared central DB stores tenant metadata, billing, and pricing.

**Tenant routing key:** JWT `tid` claim (`ms_tenant_id`).

**No `tenant_id` columns exist anywhere in per-tenant tables.** Isolation is enforced at the database level, not the row level.

```
CENTRAL DB (one shared instance)      TENANT DB (one per client)
──────────────────────────────────    ─────────────────────────────────
tenants                               users
credit_pricing                        meetings
billing_periods                       meeting_participants
invoices                              transcripts
                                      chunks  (pgvector embeddings)
                                      meeting_insights
                                      speaker_analytics
                                      video_analyses
                                      rules + rule_versions
                                      rule_violations
                                      credit_usage
                                      feature_permissions
                                      chat_sessions + chat_messages
```

---

## 1. Central DB Models (`app/db/central/models.py`)

### Tables

**`tenants`** — The routing table. Every inbound request resolves here first.
- UUID primary key with both Python default (`uuid.uuid4`) and PostgreSQL server default (`gen_random_uuid()`)
- `ms_tenant_id` — JWT `tid` claim, the routing key. Unique index.
- `org_name` — slug used in Key Vault secret name and DB name. Unique index.
- `status` — `provisioning / active / suspended / deprovisioned` enforced by CHECK constraint (not ENUM)
- `plan` — `trial / starter / pro / enterprise` enforced by CHECK constraint

**`credit_pricing`** — One row per plan defining credit rates and monthly allowances.

**`billing_periods`** — Per-tenant monthly billing windows with credit tracking.

**`invoices`** — Per-tenant invoices linked to billing periods. Status: `draft / sent / paid / void`.

### Key design decisions

| Decision | Reason |
|----------|---------|
| Separate `Base(DeclarativeBase)` from tenant models | Alembic autogenerate must not mix central and tenant tables. Two separate `Base` classes = two isolated metadata objects = safe `--autogenerate` on each side independently. |
| VARCHAR + CHECK constraints instead of PostgreSQL ENUMs | ENUMs require `ALTER TYPE` to add a value — a DDL operation that locks the table. CHECK constraints on VARCHAR columns are altered with a simple `ALTER TABLE ... ADD CONSTRAINT`, which is much safer in production. |
| Dual UUID defaults (Python + server_default) | Python default is used when creating objects in code. Server default is used for any rows inserted outside ORM (bulk inserts, seed scripts). Both sides are always consistent. |
| `DateTime(timezone=True)` everywhere | Stores UTC offset in the DB. Prevents silent timezone bugs when the server or client is in a different timezone. |
| `onupdate=func.now()` on `updated_at` | SQLAlchemy fires this on every ORM `UPDATE`. No manual timestamp management needed. |

---

## 2. Tenant DB Models (`app/db/tenant/models.py`)

### Tables (15 total)

| Table | Notes |
|-------|-------|
| `users` | `graph_id` = JWT `oid` claim. System roles: `user / admin / compliance_officer`. |
| `meetings` | `meeting_graph_id` UNIQUE — dedup key from Microsoft Graph API. |
| `meeting_participants` | **Composite PK on `(meeting_id, user_id)`** — no separate UUID id. RBAC access gate: a user can only access a meeting if they have a row here. |
| `transcripts` | One-to-one with meetings. Stores full raw text. |
| `chunks` | Text chunks for RAG retrieval. `embedding` column: `Vector(1536)` (pgvector). |
| `meeting_insights` | JSONB `fields` column — flexible per insight type (`summary`, `action_items`, etc.). |
| `speaker_analytics` | Talk time, word count, sentiment per speaker per meeting. |
| `video_analyses` | JSONB `analysis_result` — holds raw Azure Video Analyzer output. |
| `rules` | Compliance rule definitions. |
| `rule_versions` | **Append-only audit trail** — rows are never updated or deleted. |
| `rule_violations` | Links violations to the exact rule version that detected them. |
| `credit_usage` | **Append-only ledger** — rows are never updated or deleted. |
| `feature_permissions` | Per-user or per-role feature gates. |
| `chat_sessions` | Groups messages per user per meeting. |
| `chat_messages` | `citations` is JSONB — holds RAG chunk references. |

### Key design decisions

| Decision | Reason |
|----------|---------|
| Composite PK on `meeting_participants` | `(meeting_id, user_id)` uniqueness is a hard business rule — one participation record per user per meeting. Enforcing it at DB level (PK) is stronger than enforcing it in application code. A separate UUID PK would allow duplicates unless a separate UNIQUE constraint was added. |
| `granted_by` uses `ondelete="SET NULL"` | If the admin who granted access is deleted, the access grant itself should survive. `CASCADE` would silently revoke access for all users the admin ever granted, which is a security/audit problem. |
| `FeaturePermission.target_id` as `String(255)` — not a FK | `target_id` holds either a user UUID string (`target_type='user'`) or a role name string (`target_type='role'`). A FK cannot point to two different things. Plain string is the correct model for a polymorphic reference. |
| `RuleVersion` and `CreditUsage` are append-only | Compliance and billing data must never be mutated. Append-only tables guarantee a complete audit trail. |
| HNSW index **not** included in initial migration | `CREATE INDEX` on a pgvector column locks the table. `CREATE INDEX CONCURRENTLY` does not, but it cannot run inside a transaction. It must be a separate migration step, run after bulk data is loaded. |
| JSONB for `insights`, `analysis_result`, `citations`, `rule config` | These fields have flexible or evolving schemas. JSONB allows iteration without schema migrations. |

---

## 3. Central Session Factory (`app/db/central/session.py`)

### What it provides

| Export | Used by |
|--------|---------|
| `engine` | Module-level singleton — created once at import time |
| `get_central_db()` | FastAPI `Depends()` — yields a session per request |
| `central_session()` | Context manager for scripts and Celery tasks |
| `check_central_db_health()` | `/health` route |

### Pool configuration

```
pool_size=5, max_overflow=10, pool_recycle=1800, pool_pre_ping=True, pool_timeout=30
autoflush=False, expire_on_commit=False
```

### SSL enforcement

SSL is validated at the URL level — not via an event listener. If `sslmode=require` is missing from `CENTRAL_DB_URL`:
- `APP_ENV=production` → raises `ValueError` at startup (hard fail, no silent degradation)
- `APP_ENV=development` → logs a warning and continues

**Why URL-level, not event listener?** An event listener approach (`@event.listens_for(engine, "connect")`) runs a `SHOW ssl` query on every new connection. On a local PostgreSQL instance with SSL disabled, this raises a `RuntimeError` and breaks local development entirely.

---

## 4. Tenant Session Factory (`app/db/tenant/session.py`)

### What it provides

| Function | Purpose |
|----------|---------|
| `make_tenant_engine(url)` | Validates `sslmode=require` in URL, then creates engine |
| `make_tenant_session_factory(engine)` | Returns a `sessionmaker` bound to the given engine |

**No module-level engine.** Tenant session factories are created on demand by `DatabaseManager`. Creating a module-level engine would require knowing a tenant URL at import time, which is impossible in a multi-tenant system.

**Pool configuration:** `pool_size=2, max_overflow=3` per tenant. With up to 500 tenants sharing one process, a larger pool per tenant would exhaust PostgreSQL's connection limit.

---

## 5. Tenant Registry (`app/db/registry.py`)

### Purpose

In-memory cache of tenant configuration, keyed by `ms_tenant_id` (the JWT `tid` claim). Loaded at application startup. Avoids a central DB query on every inbound request.

### Design

- `CachedTenant` is a `@dataclass(frozen=True)` — immutable. Once created, no thread can modify it. Safe to share across threads without locking on reads.
- `TenantRegistry` uses an `RLock` (reentrant lock) for cache writes. `RLock` instead of `Lock` so that `refresh_one()` (which calls `put()` internally) can be called from a thread already holding the lock.
- `get()` is intentionally **lock-free** on the read path. Dict reads in CPython are safe due to the GIL, and `CachedTenant` is immutable.
- Module-level singleton: `tenant_registry = TenantRegistry()`

### Methods

| Method | Purpose |
|--------|---------|
| `load_all(session)` | Bulk-loads all tenants at startup. Replaces cache atomically. |
| `get(ms_tenant_id)` | O(1) lookup. Returns `None` if not found. |
| `put(tenant)` | Upserts one tenant into the cache. |
| `invalidate(ms_tenant_id)` | Removes one entry (e.g. after tenant is deprovisioned). |
| `refresh_one(ms_tenant_id, session)` | Re-reads one tenant from DB and updates cache. |

---

## 6. Database Manager (`app/db/manager.py`)

### Purpose

Maintains one SQLAlchemy connection pool per tenant. Handles Key Vault secret fetching, pool creation, and automatic retry on password rotation.

### Design

- Module-level singleton: `db_manager = DatabaseManager()`
- `_pool_cache: dict[str, _PoolEntry]` — one entry per tenant, created on first access
- **Double-check locking** in `_get_or_create_factory()`:
  - Outer check (no lock) — avoids acquiring the lock on the hot path for already-cached tenants
  - Inner check (under lock) — prevents two threads from simultaneously creating a pool for the same new tenant

### Password rotation retry

When PostgreSQL rejects authentication (`OperationalError` with `pgcode=28P01` or `"password authentication failed"` in the message):
1. Log a warning
2. Evict the pool entry from cache
3. Re-fetch the secret from Azure Key Vault
4. Build a new pool with the fresh secret
5. Retry the session

**Key Vault is called at most once per tenant per process lifetime** — and again only if the password is rotated. 500 tenants = 500 Key Vault calls total (at startup/first access), not per-request.

### Connection URL format

```
postgresql+psycopg2://{TENANT_DB_USER}:{secret}@{db_host}/{db_name}?sslmode=require
```

> **Open Question (CONTEXT.md §17 #3):** `db_name` currently mirrors `org_name`. The provisioning team must confirm the naming convention (e.g. `pg-{org_name}`). Change only `_db_name()` in `manager.py` and `migrate_all_tenants.py` when confirmed.

---

## 7. Alembic — Central DB (`alembic/central/env.py`)

### Configuration choices

| Setting | Value | Reason |
|---------|-------|--------|
| `poolclass` | `NullPool` | Migration is a one-shot CLI operation. A connection pool serves long-running servers. Using NullPool avoids holding idle connections open after the migration completes. |
| `transaction_per_migration` | `True` | Each migration script runs in its own transaction. If migration #5 of 10 fails, migrations #1–4 are already committed and #5 is rolled back cleanly. Without this, a failure mid-run can leave the DB in an inconsistent partial state. |
| `compare_type` | `True` | Alembic detects column type changes (e.g. `String(100)` → `String(255)`) during autogenerate. Without this, type changes are silently missed. |
| `compare_server_default` | `True` | Alembic detects server default changes during autogenerate. |

### URL source

`settings.CENTRAL_DB_URL` from `app.config.settings`. The `sqlalchemy.url` line in `alembic.ini` is intentionally blank — it is overridden at runtime by `env.py`.

---

## 8. Alembic — Tenant DB (`alembic/tenant/env.py`)

Same configuration as central, plus two tenant-specific additions:

### pgvector extension bootstrap (`_ensure_extensions`)

```python
connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
connection.commit()
```

This runs in a **separate committed transaction before any DDL migrations.** `CREATE EXTENSION` cannot run inside the same transaction as `CREATE TABLE` in some PostgreSQL versions. The explicit `connection.commit()` closes the extension transaction before the migration transaction begins.

### `render_item` hook for pgvector

Without this hook, Alembic autogenerate writes `Vector(1536)` in the migration file but never adds the import line. This causes `NameError: name 'Vector' is not defined` at migration runtime.

The hook:
1. Detects any column type with a `.dim` attribute (i.e. a `Vector` type)
2. Adds `from pgvector.sqlalchemy import Vector` to the autogenerated import block
3. Returns `f"Vector({obj.dim})"` as the string representation

It is wired into **both** `run_migrations_offline()` and `run_migrations_online()` `context.configure()` calls so it works in both dry-run and live modes.

### URL source

Set programmatically per-tenant by the caller. If `sqlalchemy.url` is not set, `_get_url()` raises a clear `RuntimeError` with instructions — not a cryptic `None` error.

---

## 9. Parallel Tenant Migration Script (`scripts/migrate_all_tenants.py`)

### Purpose

Runs `alembic upgrade head` across all active tenant databases in parallel. Used during deployments when schema changes need to be applied to all tenants.

### CLI flags

```bash
python scripts/migrate_all_tenants.py                    # all migratable tenants, 10 workers
python scripts/migrate_all_tenants.py --tenant acme      # single tenant only
python scripts/migrate_all_tenants.py --workers 20       # increase parallelism
python scripts/migrate_all_tenants.py --sql              # dry-run: print SQL, don't execute
```

### Design

| Decision | Reason |
|----------|---------|
| `_MIGRATABLE_STATUSES = {"provisioning", "active", "suspended"}` | `deprovisioned` tenants have no live DB. Attempting migration against them would fail with a connection error. |
| `ThreadPoolExecutor(max_workers=min(args.workers, len(tenants)))` | Caps workers to the actual number of tenants — no point spawning 10 threads for 3 tenants. |
| Each worker fetches its own DB session | Workers run in threads. SQLAlchemy sessions are not thread-safe. Each thread opens and closes its own `central_session()` to fetch the tenant row. |
| `_TenantSnapshot` plain dataclass | Avoids `DetachedInstanceError` — SQLAlchemy ORM objects cannot be used after their session closes. Snapshotting into a plain dataclass inside the session is the standard fix. |
| Fresh `Config` object per tenant | Alembic `Config` holds the `sqlalchemy.url`. Each tenant has a different URL. Creating a new `Config` per tenant prevents URL bleed-over between threads. |
| Exit code 1 on any failure | CI/CD pipelines detect migration failures via exit code. Logging the error alone would let a failed migration pass silently. |

---

## Open Questions (Blocking future work)

| # | Question | Impacts |
|---|----------|---------|
| 1 | Key Vault secret naming convention — is it `db-{org_name}`? | `app/core/keyvault.py` |
| 2 | DB name on PostgreSQL server — `{org_name}`, `pg-{org_name}`, or something else? | `app/db/manager.py` → `_db_name()`, `scripts/migrate_all_tenants.py` → `_db_name()` |

Both are isolated to a single `_db_name()` function in each file. Change only that function when provisioning team confirms.

---

## What Is Not Yet Done

| Item | Reason |
|------|--------|
| Actual first migration files (`alembic/*/versions/`) | Blocked on `settings.py` and `keyvault.py` being importable in the migration environment. That is Scope A work. |
| HNSW index on `chunks.embedding` | Must be `CREATE INDEX CONCURRENTLY` in a separate migration after bulk data load. Cannot be in the initial migration. |
| `app/db/helpers/` (meeting_ops, chunk_ops, vector_search, etc.) | Next phase of DB team work. Out of scope for this PR. |
