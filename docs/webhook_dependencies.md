# Webhook Service — Cross-Boundary Dependencies

This document tracks every function/class that `app/services/graph/webhook.py`
and `app/api/routes/webhook.py` **call but do not own**.

Each entry records: what is called, which file it must live in, the actual or
expected signature, and which team is responsible. Update this file whenever a
dependency is delivered or its signature changes.

---

## 1. `app/config/settings.py` — Scope A (Graph + Routes team)

| Symbol | Type | Notes |
|--------|------|-------|
| `get_settings()` | `() -> Settings` | `@lru_cache` singleton — always call this, never import `Settings` directly |
| `settings.AZURE_CLIENT_ID` | `str` | Azure app registration client ID |
| `settings.AZURE_CLIENT_SECRET` | `str` | Client secret for app-only Graph calls |
| `settings.WEBHOOK_CLIENT_STATE` | `str` | Secret validated on every incoming notification |
| `settings.WEBHOOK_BASE_URL` | `str` | Public HTTPS base URL, e.g. `https://api.vrize.com` |
| `settings.REDIS_URL` | `str` | Redis connection string — used by Celery broker |

**Status:** ✅ DELIVERED — `2026-04-20`

**Webhook import:**
```python
from app.config.settings import get_settings
settings = get_settings()
```

---

## 2. `app/services/graph/client.py` — Scope A (Graph + Routes team)

| Symbol | Signature | Notes |
|--------|----------|-------|
| `GraphClient` | `__init__(self, access_token: str)` | HTTP wrapper around Microsoft Graph API |
| `GraphClient.get` | `(path: str, params: dict = None, timeout: float = 30.0) -> dict` | GET — raises `TokenExpiredError` on 401 |
| `GraphClient.post` | `(path: str, body: dict, timeout: float = 30.0) -> dict` | POST |
| `GraphClient.patch` | `(path: str, body: dict, timeout: float = 30.0) -> dict` | PATCH |
| `GraphClient.delete` | `(path: str, timeout: float = 30.0) -> None` | DELETE — Graph returns 204, method returns None |
| `get_access_token_app` | `(ms_tenant_id: str) -> str` | Client credentials flow per customer tenant. Raises `GraphClientError` on failure. |

**Status:** ✅ DELIVERED — `2026-04-20`

**Critical note:** `get_access_token_app` accepts the **customer's** `ms_tenant_id`,
NOT `settings.AZURE_TENANT_ID`. Per-tenant MSAL app cache is built in.
Confirmed multi-tenant by user on 2026-04-20.

**Webhook import:**
```python
from app.services.graph.client import GraphClient, get_access_token_app
```

---

## 3. `app/services/graph/exceptions.py` — Scope A (Graph + Routes team)

| Symbol | Signature | Notes |
|--------|----------|-------|
| `GraphClientError` | `__init__(self, message: str, status_code: int \| None = None)` | All non-401 Graph failures + network errors. `status_code=None` for network-level failures. |
| `TokenExpiredError` | `Exception` subclass | Raised on Graph 401 — delegated token expired |
| `MeetingNotFoundError` | `__init__(self, message: str)` | Graph responded OK but returned empty result set |

**Status:** ✅ DELIVERED — `2026-04-20`

**Webhook import:**
```python
from app.services.graph.exceptions import GraphClientError, TokenExpiredError
```

---

## 4. `app/db/central/models.py` — DB team

| Symbol | Type | Notes |
|--------|------|-------|
| `Tenant` | SQLAlchemy `DeclarativeBase` model | Maps to the `tenants` table in the central DB |
| `Tenant.id` | `UUID` | Primary key |
| `Tenant.org_name` | `str` | Unique slug — passed to Celery as task argument |
| `Tenant.ms_tenant_id` | `str` | Azure AD tenant GUID — **the lookup key from webhook notification** |
| `Tenant.status` | `str` | `'active'` / `'suspended'` / `'deprovisioned'` / `'provisioning'` |

**Status:** ⏳ PENDING — DB team

**How webhook uses it:**
```python
from app.db.central.models import Tenant

tenant = db.query(Tenant).filter(
    Tenant.ms_tenant_id == notification_tenant_id
).first()
```

---

## 5. `app/db/central/session.py` — DB team

| Symbol | Signature | Notes |
|--------|----------|-------|
| `get_central_db` | `() -> Generator[Session, None, None]` | FastAPI dependency — yields a SQLAlchemy `Session` to the central DB, closes on exit |

**Status:** ⏳ PENDING — DB team

**How webhook uses it:**
```python
from app.db.central.session import get_central_db

@router.post("/call-records")
def call_records(
    db: Session = Depends(get_central_db),
    ...
):
```

---

## 6. `workers/celery_app.py` — Workers team

| Symbol | Type | Notes |
|--------|------|-------|
| `celery_app` | `celery.Celery` instance | Configured with `broker=settings.REDIS_URL`, `backend=settings.REDIS_URL` |

**Status:** ⏳ PENDING — Workers team

**How webhook uses it:**
```python
from workers.celery_app import celery_app

celery_app.send_task(
    "workers.tasks.ingestion.ingest_meeting_task",
    args=[call_chain_id, org_name],
)
```

Note: webhook uses `send_task` by **task name string** — it does NOT import the
task function directly. This avoids circular imports and keeps team boundaries clean.

---

## 7. `workers/tasks/ingestion.py` — Workers team

| Symbol | Signature | Notes |
|--------|----------|-------|
| `ingest_meeting_task` | `(call_chain_id: str, org_name: str) -> None` | Celery task — fetches callRecord from Graph, parses VTT, embeds, writes to tenant DB, fans out to insights + sentiment + rules |

**Registered task name must be exactly:** `"workers.tasks.ingestion.ingest_meeting_task"`

**Status:** ⏳ PENDING — Workers team

**Arguments the webhook passes:**
- `call_chain_id` — Graph call chain ID extracted from `notification["resource"]`
- `org_name` — `Tenant.org_name` from central DB — worker uses this to resolve the tenant DB connection

---

---

## 8. `app/core/security.py` — Scope A (Graph + Routes team)

| Symbol | Type | Notes |
|--------|------|-------|
| `CurrentUser` | Pydantic `BaseModel` | Injected into admin routes by `require_admin` dependency |
| `CurrentUser.tenant.ms_tenant_id` | `str` | Customer's Azure AD tenant GUID — used as `ms_tenant_id` in all webhook service calls (Option B) |
| `CurrentUser.tenant.org_name` | `str` | Customer's org slug — passed to service functions for logging + Celery dispatch |

**Status:** ✅ DELIVERED — `2026-04-20`

**Webhook import:**
```python
from app.core.security import CurrentUser
```

---

## 9. `app/api/deps.py` — Scope A (Graph + Routes team)

| Symbol | Signature | Notes |
|--------|----------|-------|
| `get_current_user` | `() -> CurrentUser` | FastAPI dependency — validates JWT, resolves tenant, returns `CurrentUser` |
| `require_admin` | `(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser` | FastAPI dependency — calls `get_current_user` then asserts `system_role == 'admin'`; raises HTTP 403 otherwise |
| `get_central_db` | `() -> Generator[Session, None, None]` | FastAPI dependency — yields SQLAlchemy Session to central DB (mirrors entry in section 5 above) |

**Status:** ⏳ PENDING — Scope A team

**Webhook import (route file):**
```python
from app.api.deps import require_admin, get_current_user
```

**How webhook uses it:**
```python
@router.post("/register")
def register_webhook(
    current_user: CurrentUser = Depends(require_admin),
):
    ms_tenant_id = current_user.tenant.ms_tenant_id
    org_name = current_user.tenant.org_name
```

---

## Summary table

| File | Symbol(s) | Team | Status |
|------|-----------|------|--------|
| `app/config/settings.py` | `get_settings()` | Scope A | ✅ Delivered |
| `app/services/graph/client.py` | `GraphClient`, `get_access_token_app(ms_tenant_id)` | Scope A | ✅ Delivered |
| `app/services/graph/exceptions.py` | `GraphClientError`, `TokenExpiredError` | Scope A | ✅ Delivered |
| `app/core/security.py` | `CurrentUser`, `TenantInfo` | Scope A | ✅ Delivered |
| `app/db/central/models.py` | `Tenant` model | DB team | ⏳ Pending |
| `app/db/central/session.py` | `get_central_db` | DB team | ⏳ Pending |
| `app/api/deps.py` | `require_admin`, `get_current_user` | Scope A | ⏳ Pending |
| `workers/celery_app.py` | `celery_app` | Workers team | ⏳ Pending |
| `workers/tasks/ingestion.py` | `ingest_meeting_task` | Workers team | ⏳ Pending |

---

*Last updated: 2026-04-20*
*Owner of this document: Webhook team (Yash)*
*Update this file whenever a dependency is delivered or its signature changes.*
