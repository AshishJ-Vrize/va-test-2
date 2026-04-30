# VA Platform — Production Architecture Context

**Read this before writing a single line of code.**  
Every decision here was explicitly confirmed in a team architecture session.  
Nothing is assumed. Anything not confirmed is labelled `[OPEN QUESTION — DO NOT IMPLEMENT]`.

---

## 1. What This Product Is

Multi-tenant SaaS. Ingests Microsoft Teams meeting transcripts, runs AI analysis,
provides a RAG chatbot and compliance engine.

- **Backend:** FastAPI + PostgreSQL + Azure services
- **Frontend:** Next.js 14 (React) — MSAL.js handles user authentication client-side
- **Task queue:** Celery + Azure Redis
- **Infrastructure:** Azure (Container Apps, PostgreSQL Flexible Server, Redis Cache,
  Key Vault, Blob Storage)

---

## 2. Final File Structure

```
va-platform/
├── app/
│   ├── __init__.py
│   ├── main.py                        # FastAPI app, lifespan, routers
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py                # Pydantic BaseSettings — all env vars
│   │
│   ├── core/                          # Infrastructure concerns only
│   │   ├── __init__.py
│   │   ├── security.py                # JWT verification, JWKS cache, CurrentUser model
│   │   └── keyvault.py                # Azure Key Vault client, get_db_secret()
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py                    # FastAPI dependencies: get_current_user,
│   │   │                              # get_tenant_db, get_central_db, require_admin
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   ├── tenant.py              # Extracts tid from JWT, resolves tenant,
│   │   │   │                          # attaches to request.state
│   │   │   ├── rate_limit.py          # Per-tenant rate limiting
│   │   │   └── timeout.py             # Request timeout (30s)
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── auth.py                # POST /auth/me
│   │       ├── meetings.py            # GET /meetings, POST /meetings/sync
│   │       ├── webhook.py             # POST /webhook/register, /webhook/call-records
│   │       ├── ingest.py              # POST /ingest/meeting
│   │       ├── query.py               # POST /query
│   │       ├── chat.py                # POST /chat
│   │       ├── rules.py               # CRUD /rules, GET /violations
│   │       ├── admin.py               # /admin/* routes
│   │       └── health.py              # GET /health (no auth)
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── manager.py                 # DatabaseManager — tenant connection pool cache
│   │   ├── registry.py                # TenantRegistry — in-memory tenant config cache
│   │   ├── central/
│   │   │   ├── __init__.py
│   │   │   ├── models.py              # Tenant, CreditPricing, BillingPeriod, Invoice
│   │   │   └── session.py             # Central DB engine + session factory
│   │   ├── tenant/
│   │   │   ├── __init__.py
│   │   │   ├── models.py              # All 15 per-tenant tables
│   │   │   └── session.py             # Tenant session factory (used by manager.py)
│   │   └── helpers/
│   │       ├── __init__.py
│   │       ├── meeting_ops.py
│   │       ├── transcript_ops.py
│   │       ├── chunk_ops.py
│   │       ├── vector_search.py
│   │       ├── insight_ops.py
│   │       ├── sentiment_ops.py
│   │       ├── speaker_ops.py
│   │       ├── rules_ops.py
│   │       ├── credit_ops.py
│   │       └── admin_ops.py
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── graph/
│   │   │   ├── __init__.py
│   │   │   ├── client.py              # GraphClient, TokenExpiredError,
│   │   │   │                          # get_access_token_app(ms_tenant_id)
│   │   │   ├── meetings.py            # Graph meeting methods
│   │   │   ├── transcripts.py         # Graph transcript methods
│   │   │   └── webhook.py             # Webhook registration — NOT in this scope
│   │   ├── ingestion/
│   │   │   ├── __init__.py
│   │   │   ├── pipeline.py
│   │   │   ├── vtt_parser.py
│   │   │   ├── chunker.py
│   │   │   └── embedder.py
│   │   ├── chat/
│   │   │   ├── __init__.py
│   │   │   ├── orchestrator.py
│   │   │   ├── router.py
│   │   │   ├── prompts.py
│   │   │   ├── meta_handler.py
│   │   │   ├── structured_handler.py
│   │   │   ├── search_handler.py
│   │   │   └── hybrid_handler.py
│   │   ├── insights/
│   │   │   ├── __init__.py
│   │   │   ├── generator.py
│   │   │   ├── parser.py
│   │   │   └── prompts.py
│   │   ├── sentiment/
│   │   │   ├── __init__.py
│   │   │   ├── analyzer.py
│   │   │   └── aggregator.py
│   │   └── rules/
│   │       ├── __init__.py
│   │       ├── orchestrator.py
│   │       ├── extractor.py
│   │       ├── engine.py
│   │       ├── schemas.py
│   │       ├── prompts.py
│   │       └── evaluators/
│   │           ├── __init__.py
│   │           ├── keyword.py
│   │           ├── speaker_keyword.py
│   │           ├── profanity_threshold.py
│   │           ├── risk_severity.py
│   │           ├── missing_action_items.py
│   │           ├── speaker_dominance.py
│   │           ├── meeting_duration.py
│   │           ├── sentiment_threshold.py
│   │           ├── camera_presence.py
│   │           ├── emotion_threshold.py
│   │           └── llm_freeform.py
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logger.py
│       └── token_counter.py
│
├── workers/
│   ├── __init__.py
│   ├── celery_app.py
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── ingestion.py
│   │   ├── insights.py
│   │   ├── sentiment.py
│   │   ├── speaker_analysis.py
│   │   ├── video_analysis.py
│   │   ├── rules.py
│   │   ├── credit.py
│   │   └── billing.py
│   └── beat/
│       ├── __init__.py
│       └── schedules.py
│
├── provisioning/
│   ├── __init__.py
│   ├── provision_tenant.py
│   ├── deprovision_tenant.py
│   └── scale_tenant.py
│
├── alembic/
│   ├── central/
│   │   ├── alembic.ini
│   │   ├── env.py
│   │   └── versions/
│   └── tenant/
│       ├── alembic.ini
│       ├── env.py
│       └── versions/
│
├── scripts/
│   ├── migrate_all_tenants.py
│   ├── bootstrap_admin.py
│   ├── rechunk_meeting.py
│   └── backfill_embeddings.py
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_chunker.py
│   ├── test_ingestion.py
│   ├── test_vector_search.py
│   ├── test_tenant_routing.py
│   └── test_rules_engine.py
│
├── .env.example
├── requirements.txt
├── Dockerfile
├── Dockerfile.worker
└── docker-compose.yml
```

---

## 3. Team Scope Boundaries

**Understand this section before touching any file.**

### Scope A — Graph + Routes (this context file's primary concern)

| File | Owner scope |
|------|-------------|
| `app/core/security.py` | Scope A |
| `app/core/keyvault.py` | Scope A |
| `app/api/deps.py` | Scope A |
| `app/api/middleware/tenant.py` | Scope A |
| `app/api/routes/auth.py` | Scope A |
| `app/api/routes/meetings.py` | Scope A |
| `app/api/routes/ingest.py` | Scope A |
| `app/api/routes/query.py` | Scope A |
| `app/api/routes/chat.py` | Scope A |
| `app/api/routes/rules.py` | Scope A |
| `app/api/routes/admin.py` | Scope A |
| `app/api/routes/health.py` | Scope A |
| `app/services/graph/client.py` | Scope A |
| `app/services/graph/meetings.py` | Scope A |
| `app/services/graph/transcripts.py` | Scope A |

### Out of Scope A — Do Not Modify

| File | Reason |
|------|--------|
| `app/services/graph/webhook.py` | Webhook team |
| `app/api/routes/webhook.py` | Webhook team |
| `app/api/middleware/rate_limit.py` | Infrastructure team |
| `app/api/middleware/timeout.py` | Infrastructure team |
| `app/db/**` | DB team |
| `app/services/ingestion/**` | Ingestion team |
| `app/services/chat/**` | Chat team |
| `app/services/insights/**` | Insights team |
| `app/services/sentiment/**` | Sentiment team |
| `app/services/rules/**` | Rules team |
| `workers/**` | Workers team |
| `provisioning/**` | DevOps team |
| `alembic/**` | DB team |

---

## 4. Multi-Tenancy Model — Confirmed

**Strategy:** DB-per-tenant.  
Each client gets an isolated Azure PostgreSQL Flexible Server instance.  
One shared central DB stores tenant metadata, billing, and pricing.

**Tenant identification method:** JWT `tid` claim — confirmed as the routing key.

```
CENTRAL DB (shared, one instance)     TENANT DB (isolated, one per client)
──────────────────────────────────    ──────────────────────────────────────
tenants                               users
credit_pricing                        meetings
billing_periods                       meeting_participants
invoices                              transcripts
                                      chunks  (pgvector)
                                      meeting_insights
                                      speaker_analytics
                                      video_analyses
                                      rules + rule_versions
                                      rule_violations
                                      credit_usage
                                      feature_permissions
                                      chat_sessions + chat_messages
```

**No `tenant_id` columns exist in any per-tenant table.**  
The entire database belongs to one tenant. Isolation is at the database level.

---

## 5. Central DB — `tenants` Table

This is the routing table. Every inbound request resolves here first.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Internal tenant identifier |
| `org_name` | VARCHAR(100) UNIQUE | Slug — used in Key Vault secret name and DB name |
| `display_name` | VARCHAR(255) | Human-readable company name |
| `ms_tenant_id` | VARCHAR(255) UNIQUE | **JWT `tid` claim — the routing key** |
| `db_host` | VARCHAR(500) | e.g. `pg-acme.postgres.database.azure.com` |
| `db_region` | VARCHAR(50) | Azure region |
| `db_sku` | VARCHAR(50) | PostgreSQL SKU |
| `blob_container` | VARCHAR(255) | Azure Blob container name |
| `status` | VARCHAR(20) | `provisioning` / `active` / `suspended` / `deprovisioned` |
| `plan` | VARCHAR(50) | `trial` / `starter` / `pro` / `enterprise` |
| `max_users` | INTEGER | User limit per plan |
| `max_meetings_per_month` | INTEGER NULL | NULL = unlimited |
| `onboarded_at` | TIMESTAMP | |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | |

**Only `status = 'active'` tenants are allowed through.**  
`suspended` or `deprovisioned` → 403 before any tenant DB is touched.

---

## 6. Per-Tenant DB — Key Tables

### `users`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | Internal user ID — used in all FK references |
| `graph_id` | VARCHAR(255) UNIQUE | JWT `oid` claim — never changes |
| `email` | VARCHAR(320) | UPN |
| `display_name` | VARCHAR(255) | |
| `system_role` | VARCHAR(20) | `user` / `admin` / `compliance_officer` |
| `is_active` | BOOLEAN | Default TRUE |
| `last_login_at` | TIMESTAMP NULL | Updated on every authenticated request |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | |

### `meetings` (columns relevant to graph/routes)

| Column | Notes |
|--------|-------|
| `id` | UUID PK |
| `meeting_graph_id` | VARCHAR(255) UNIQUE — Graph API meeting ID, dedup key |
| `organizer_id` | FK → users.id |
| `meeting_subject` | VARCHAR(500) |
| `meeting_date` | TIMESTAMP — start datetime |
| `meeting_end_date` | TIMESTAMP — end datetime |
| `duration_minutes` | INTEGER — precomputed, stored |
| `join_url` | VARCHAR(2000) UNIQUE — dedup on re-ingest |
| `ingestion_source` | VARCHAR(20) — `manual` / `webhook` |
| `status` | VARCHAR(20) — `pending` / `ingesting` / `ready` / `failed` |

### `meeting_participants` — the RBAC access gate

| Column | Notes |
|--------|-------|
| `meeting_id` | FK → meetings.id |
| `user_id` | FK → users.id |
| `role` | `organizer` / `attendee` / `granted` |
| `granted_by` | FK → users.id, SET NULL on delete |

A user can only access a meeting if they have a row in this table.
No exceptions. Even admins go through this gate for the chat endpoint.

### `feature_permissions`

| Column | Notes |
|--------|-------|
| `target_type` | `user` or `role` |
| `target_id` | user UUID string or role name string |
| `feature_key` | `chat` / `rules_management` / `insights_view` / `sentiment_view` / `video_analytics` / `compliance_dashboard` / `user_management` |
| `permission` | `allow` / `deny` |
| `granted_by` | FK → users.id |

**Evaluation order (first match wins):**
1. User-specific row (`target_type = 'user'`, `target_id = user.id`)
2. Role row (`target_type = 'role'`, `target_id = user.system_role`)
3. Default: deny for regular users, allow-all for `admin` role

---

## 7. Authentication — Confirmed Decisions

### User auth (frontend → backend)
- **Flow:** OAuth2 Authorization Code with PKCE
- **Handled by:** MSAL.js in Next.js frontend — backend never initiates user auth
- **Authority:** `https://login.microsoftonline.com/common` (multi-tenant)
- **App registration:** Single multi-tenant Azure app registration
- **What frontend sends:** Bearer token in `Authorization` header on every request
- **Device code flow:** Removed entirely — it was MVP only
- **Disk token cache (`.token_cache.json`):** Removed entirely

### App auth (backend → Microsoft Graph, for ALL Graph calls)
- **Flow:** Client credentials (app-only) — confirmed as the only Graph token strategy
- **Function:** `get_access_token_app(ms_tenant_id: str)` in `app/services/graph/client.py`
- **Scope requested:** `https://graph.microsoft.com/.default`
- **Used for:** All Graph API calls — webhook registration, callRecords, meeting lookup, transcript fetch
- **Path rule:** Always use `/users/{user_id}/onlineMeetings/...` — never `/me/` (app tokens return 400 on /me/)
- **OBO flow:** Not used. Resolved 2026-04-22 — see Section 18 Q1.

### Token cache
- **Store:** Azure Redis Cache — confirmed
- MSAL token cache: NOT on disk
- JWKS cache: Redis, TTL 1 hour (see Section 8)

---

## 8. JWT Validation — Verified Values

**Source:** Live OIDC discovery document + real decoded token.  
Do not change these values without re-verification against a live token.

### Verified OIDC values

| Field | Verified value |
|-------|---------------|
| OIDC discovery URL | `https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration` |
| JWKS URI | `https://login.microsoftonline.com/common/discovery/v2.0/keys` |
| Token signing algorithm | RS256 |
| Issuer format | `https://login.microsoftonline.com/{tenantid}/v2.0` |

### Verified token claims

| Claim | Verified value | Usage |
|-------|---------------|-------|
| `iss` | `https://login.microsoftonline.com/{actual-tid-guid}/v2.0` | Constructed per-token, NOT static |
| `aud` | `AZURE_CLIENT_ID` (the app's client ID) | Validated as `settings.AZURE_CLIENT_ID` |
| `tid` | Azure tenant GUID | Maps to `tenants.ms_tenant_id` in central DB |
| `oid` | User object GUID | Maps to `users.graph_id` in tenant DB |
| `ver` | `"2.0"` | Reject tokens where `ver != "2.0"` |

### Why `iss` cannot be a static string

The OIDC discovery doc shows `{tenantid}` as a placeholder.  
In a real token, `iss` contains the actual tenant GUID of the signing tenant.  
In a multi-tenant app, each tenant's token has a different `iss`.  
Validating against a hardcoded string will reject every token except your own.

### Mandatory JWT validation sequence

Execute in this exact order. Do not skip steps or reorder.

```
1. Extract token from Authorization header: "Bearer <token>"
   → If missing or malformed: 401

2. Decode token WITHOUT signature verification
   → Extract: tid, kid (key ID from header)
   → If tid is empty or missing: 401

3. Look up tenants.ms_tenant_id = tid in central DB (via TenantRegistry — in-memory)
   → If not found: 401 "Unknown tenant"
   → If status = 'suspended' or 'deprovisioned': 403 "Tenant not active"
   → If status = 'provisioning': 403 "Tenant not ready"

4. Fetch JWKS from: https://login.microsoftonline.com/common/discovery/v2.0/keys
   → Check Redis cache first (key: "jwks_cache", TTL: 3600 seconds)
   → If cache miss: fetch from URL, store in Redis
   → Find the key object where key["kid"] == kid from step 2
   → If kid not found in JWKS: 401 "Unknown signing key"

5. Verify full token using the matched key:
   - Signature (RS256)
   - aud == settings.AZURE_CLIENT_ID
   - iss == f"https://login.microsoftonline.com/{tid}/v2.0"
   - exp > now (not expired)
   → Any failure: 401

6. Extract verified claims: oid, preferred_username (or email), name

7. Upsert user in tenant DB:
   - Look up users.graph_id = oid
   - If not found: INSERT new user row (display_name from token claims)
   - Update users.last_login_at = now()

8. Return CurrentUser object (see Section 9)
```

**Library:** `python-jose[cryptography]` — already in requirements.txt  
**Do NOT use:** PyJWT alone (no RS256 JWKS support out of the box)

---

## 9. Data Models for Scope A

### `CurrentUser`
Returned by `get_current_user` dependency. Passed to all route handlers.

```python
class CurrentUser(BaseModel):
    id: UUID                  # users.id in tenant DB
    graph_id: str             # oid claim from JWT
    tid: str                  # tid claim = tenants.ms_tenant_id
    email: str | None         # preferred_username or email claim
    display_name: str         # name claim, falls back to email prefix
    system_role: str          # 'user' | 'admin' | 'compliance_officer'
    is_active: bool           # users.is_active
    tenant: TenantInfo        # from central DB tenants row
```

### `TenantInfo`
Resolved from central DB during JWT validation.

```python
class TenantInfo(BaseModel):
    id: UUID                  # tenants.id
    org_name: str             # tenants.org_name
    db_host: str              # tenants.db_host
    ms_tenant_id: str         # tenants.ms_tenant_id
    status: str               # always 'active' if CurrentUser was issued
    plan: str                 # tenants.plan
```

---

## 10. Tenant DB Routing — Confirmed

### Credential storage
- **Method:** Azure Key Vault — confirmed
- **No password column in `tenants` table** — credentials never in any DB
- **Secret naming convention:** `db-{org_name}` — proposed, pending team confirmation
  ⚠ If provisioning team uses a different convention, update `keyvault.py` only

### DB username
- Shared across all tenant DBs
- Stored in environment variable: `TENANT_DB_USER`
- Per-tenant isolation is at the password level, not the username level

### Connection string template
```
postgresql+psycopg2://{TENANT_DB_USER}:{secret}@{db_host}/{db_name}?sslmode=require
```
Where `db_name` is the actual database name on the PostgreSQL server.  
⚠ `db_name` naming convention (e.g. `pg-{org_name}`) to be confirmed with provisioning team.

### Connection pool settings (per tenant)
```
pool_size=2
max_overflow=3          # max 5 connections per tenant
pool_recycle=1800       # recycle idle connections after 30 min
pool_pre_ping=True      # health-check before use
```

### Pool cache behaviour
```
First request from tenant X:
  → Fetch secret from Key Vault ("db-{org_name}")
  → Create SQLAlchemy Engine
  → Store in _pool_cache[tid]
  → Yield Session

All subsequent requests from tenant X:
  → _pool_cache[tid] exists → skip Key Vault
  → Yield Session from existing pool

On PostgreSQL auth error (password rotated in Key Vault):
  → Delete _pool_cache[tid]
  → Re-fetch secret from Key Vault
  → Recreate Engine
  → Retry once
```

### Scale characteristics
- 500 tenants → 500 Key Vault calls total (lifetime), not per-request
- Key Vault Standard: 2000 GET operations per 10 seconds — not a bottleneck
- Memory: 500 tenants × 5 max connections = 2500 connections max

---

## 11. FastAPI Dependencies — `app/api/deps.py`

```python
# Signatures only — implementation detail is in deps.py

def get_central_db() -> Generator[Session, None, None]:
    """Yields a session to the central DB (tenants, billing, pricing)."""

def get_current_user(
    token: str = Depends(oauth2_scheme),
    central_db: Session = Depends(get_central_db),
) -> CurrentUser:
    """Full JWT validation. Returns CurrentUser. Raises 401/403 on failure."""

def get_tenant_db(
    current_user: CurrentUser = Depends(get_current_user),
) -> Generator[Session, None, None]:
    """Resolves tenant DB from current_user.tenant. Uses pool cache."""

def require_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Raises 403 if current_user.system_role != 'admin'."""

def require_feature(feature_key: str) -> Callable:
    """
    Returns a dependency factory.
    Checks feature_permissions table for current user + role.
    Raises 403 if denied.
    """
```

**Chaining:**
```
require_admin
  └── get_current_user
        └── get_central_db

get_tenant_db
  └── get_current_user
        └── get_central_db
```

---

## 12. Middleware Behaviour — `app/api/middleware/tenant.py`

**Runs on every request** except paths listed in PUBLIC_PATHS.

```python
PUBLIC_PATHS = [
    "/api/v1/health",
    "/api/v1/webhook/call-records",  # Called by Microsoft, not users
    "/docs",
    "/openapi.json",
    "/redoc",
]
```

**What tenant.py attaches to `request.state`:**
```
request.state.tid              # str  — JWT tid claim (unverified at middleware stage)
request.state.tenant           # TenantInfo | None
request.state.user_graph_id    # str  — JWT oid claim (unverified at middleware stage)
```

**Important:** Middleware does a fast UNVERIFIED decode to extract `tid` for early
rejection of unknown tenants. Full signature verification happens in
`get_current_user` dependency (`app/core/security.py`).  
Never trust `request.state` values without going through `get_current_user`.

---

## 13. Graph API — `app/services/graph/`

### `client.py` — exports

```python
GRAPH_BASE = "https://graph.microsoft.com/v1.0"  # verified

class TokenExpiredError(Exception):
    """Raised on HTTP 401 from Graph API. Caught by route handlers."""

class GraphClient:
    def __init__(self, access_token: str): ...
    def get(self, path: str, params: dict = None) -> dict: ...
    def post(self, path: str, body: dict) -> dict: ...
    def patch(self, path: str, body: dict) -> dict: ...
    def delete(self, path: str) -> None: ...
    def _base_path(self, user_id: str = None) -> str: ...
    # /me/onlineMeetings       when user_id is None  (delegated token)
    # /users/{id}/onlineMeetings  when user_id given (app token)

def get_access_token_app(ms_tenant_id: str) -> str:
    """
    Client credentials flow. Returns access token scoped to the given tenant.
    Authority: https://login.microsoftonline.com/{ms_tenant_id}
    Uses: settings.AZURE_CLIENT_ID, settings.AZURE_CLIENT_SECRET
    Scope: https://graph.microsoft.com/.default
    Used by: webhook service only.

    ms_tenant_id is the CUSTOMER's Azure tenant ID (tenants.ms_tenant_id),
    NOT settings.AZURE_TENANT_ID (which is the platform's own tenant).
    Same client_id + client_secret, different authority per customer tenant.
    Admin consent for CallRecords.Read.All must be granted in each customer tenant.
    """
```

### Verified ingestion flow (MVP + live testing 2026-04-21)

```
callChainId → joinWebUrl  (webhook team resolves this)
  → get_meeting_by_join_url(joinWebUrl)  → meetingId + participants (displayName=null)
  → get_user_by_id(email/oid)            → real displayName per participant
  → get_transcripts(meetingId)           → transcriptId
  → get_transcript_content(meetingId, transcriptId) → raw VTT string
```

### `meetings.py` — exports

```python
# All methods are on GraphClient. These are defined in meetings.py.

GraphClient.get_me(self) -> dict:
    # GET /me
    # Returns: id, displayName, mail, userPrincipalName
    # Delegated token only. Fails with app token.

GraphClient.get_user_by_id(self, user_graph_id: str) -> dict | None:
    # GET /users/{user_graph_id}
    # Returns full user profile or None on 404
    # Passing UPN (email) as user_graph_id works as lookup key
    # displayName in meeting participant responses is ALWAYS null —
    # call this to get the real display name for each participant

GraphClient.get_online_meeting(self, meeting_id: str, user_id: str = None) -> dict:
    # GET /me/onlineMeetings/{meeting_id}           (delegated)
    # GET /users/{user_id}/onlineMeetings/{meeting_id}  (app token)

GraphClient.get_meeting_by_join_url(self, join_url: str, user_id: str = None) -> dict:
    # GET /me/onlineMeetings?$filter=joinWebUrl eq '{join_url}'
    # ⚠ $filter is REQUIRED — this endpoint does not support listing all meetings.
    # Verified live 2026-04-21: calling without $filter returns 400 InvalidArgument.
    # Returns first result or raises MeetingNotFoundError if none found.

# list_online_meetings REMOVED — GET /me/onlineMeetings is not a list endpoint.
# It requires $filter. Verified live 2026-04-21 (400 InvalidArgument without filter).
# Meetings are discovered via webhook callChainId → joinWebUrl, not by listing.
```

### `transcripts.py` — exports

```python
GraphClient.get_transcripts(self, meeting_id: str, user_id: str = None) -> list[dict]:
    # GET /me/onlineMeetings/{meeting_id}/transcripts
    # Returns empty list if transcript not ready yet — not an error

GraphClient.get_transcript_content(
    self, meeting_id: str, transcript_id: str, user_id: str = None
) -> str:
    # GET .../transcripts/{transcript_id}/content?$format=text/vtt
    # Returns raw VTT string
    # Timeout: 60 seconds (larger than standard — content can be large)
```

### `webhook.py` — NOT in this scope

This file is owned by the webhook team. Do not modify it.  
The route handler in `app/api/routes/webhook.py` calls functions from this file
but does not implement them.

### Graph API participant response — verified shape

```json
{
  "participants": {
    "organizer": {
      "upn": "john.doe@company.com",
      "identity": {
        "user": {
          "id": "abc-123-guid",
          "displayName": null
        }
      }
    },
    "attendees": [
      {
        "upn": "jane.smith@company.com",
        "identity": {
          "user": {
            "id": "def-456-guid",
            "displayName": null
          }
        }
      }
    ]
  }
}
```

`displayName` is **always null** in meeting participant responses.  
To get display name: call `get_user_by_id(upn)`.  
The UPN (email) works as the lookup key in `GET /users/{upn}`.

---

## 14. Route Handler Rules

1. **No business logic in route handlers.** Routes validate input, call a service or
   helper, and return output. Nothing else.

2. **No plain `admin_user_id` string parameters.** Every admin check uses
   `current_user: CurrentUser = Depends(require_admin)`. This is a hard break from
   the MVP pattern.

3. **Webhook endpoint has no auth dependency.** `POST /webhook/call-records` is
   called by Microsoft Graph, not by users. It has no `get_current_user` dependency.
   It handles:
   - `?validationToken=` query param present → echo it as plain text (handshake)
   - No validationToken → dispatch to webhook service (background)

4. **Chat endpoint has no admin bypass on the participant gate.** Even admins only
   see meetings where they have a `meeting_participants` row. This is enforced at the
   service/helper level, not at the route level.

5. **All DB commits happen in route handlers**, not in service functions.  
   Services do the work; routes commit.

6. **RBAC hierarchy:**
   - `admin` — full access to admin endpoints, can modify rules, grant access
   - `compliance_officer` — can view violations and compliance data, cannot modify rules
   - `user` — can access meetings they participated in, can use chat

---

## 15. Required Environment Variables

```
# Azure App Registration (single multi-tenant)
AZURE_CLIENT_ID=          # Application (client) ID
AZURE_CLIENT_SECRET=      # Client secret — for app-only Graph calls (webhooks) only
AZURE_TENANT_ID=          # Your own tenant ID — used only in get_access_token_app()

# Central Database
CENTRAL_DB_URL=           # postgresql+psycopg2://user:pass@host/central_db_name

# Tenant DB auth
TENANT_DB_USER=           # PostgreSQL username shared across all tenant DBs

# Azure Key Vault
AZURE_KEYVAULT_URL=       # https://{vault-name}.vault.azure.net

# Redis (JWKS cache + Celery broker + MSAL cache)
REDIS_URL=                # rediss://:password@host:6380/0

# Webhook
WEBHOOK_BASE_URL=         # Public HTTPS URL this backend is reachable at
WEBHOOK_CLIENT_STATE=     # Secret string to validate Graph notifications

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT_EMBEDDING=   # text-embedding-3-small
AZURE_OPENAI_DEPLOYMENT_LLM=         # gpt-4o

# Azure Text Analytics
AZURE_TEXT_ANALYTICS_ENDPOINT=
AZURE_TEXT_ANALYTICS_KEY=

# Sentry (optional)
SENTRY_DSN=
```

---

## 16. Verified External Endpoints

Only these endpoints are confirmed verified. Do not use any other Graph or Microsoft
endpoint without verifying the URI and response shape first.

| Service | URL | Confirmed |
|---------|-----|-----------|
| OIDC discovery | `https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration` | ✓ |
| JWKS | `https://login.microsoftonline.com/common/discovery/v2.0/keys` | ✓ |
| Token endpoint | `https://login.microsoftonline.com/common/oauth2/v2.0/token` | ✓ |
| Graph API base | `https://graph.microsoft.com/v1.0` | ✓ |
| `GET /me` | `https://graph.microsoft.com/v1.0/me` | ✓ |
| `GET /users/{id}` | `https://graph.microsoft.com/v1.0/users/{id}` | ✓ |
| Online meetings | `https://graph.microsoft.com/v1.0/me/onlineMeetings` | ✓ |
| Transcripts list | `https://graph.microsoft.com/v1.0/me/onlineMeetings/{id}/transcripts` | ✓ |
| Transcript content | `https://graph.microsoft.com/v1.0/me/onlineMeetings/{id}/transcripts/{tid}/content` | ✓ |

---

## 17. Graph API — Error Handling and Retry Policy

**Implemented in:** `app/services/graph/client.py` → `GraphClient._request()`  
**Exceptions defined in:** `app/services/graph/exceptions.py`

### Retry targets (automatic, up to 3 attempts)

| Failure | Strategy | Notes |
|---------|----------|-------|
| Network error / timeout | Exponential backoff + jitter: ~1s, ~2s, ~4s | Jitter prevents thundering herd across tenants |
| 429 Too Many Requests | Wait `Retry-After` header value; fall back to backoff if header absent | Graph always includes this header |
| 5xx Server Error | Exponential backoff + jitter: ~1s, ~2s, ~4s | Graph outage — retries buy time for recovery |

**After 3 retries exhausted:**
- Route handlers → return HTTP 503 to frontend ("Graph temporarily unavailable")
- Celery tasks → Celery reschedules with long delay (5–60 min). Celery owns extended outage resilience, not `_request()`.

### No-retry targets (fail immediately)

| Status | Action | Reason |
|--------|--------|--------|
| 401 Unauthorized | Raise `TokenExpiredError` | Delegated token expired. Frontend must re-auth via MSAL.js. Backend cannot refresh delegated tokens. |
| 400 Bad Request | Raise `GraphClientError`, log method + URL + params + body + `graph_code` + `graph_message` | This is a code bug. Full request logged for debugging. Never retry — same bad request will fail again. |
| 403 Forbidden | Raise `GraphClientError` with `graph_code` + `likely_cause` | Permission or consent issue — requires human/admin action. |
| 404 Not Found | Raise `GraphClientError` with `graph_code` + `likely_cause` | Resource does not exist. Common causes logged per endpoint (see below). |

### 401 token handling — delegated vs app-only

| Token type | 401 behaviour |
|------------|--------------|
| Delegated (user, from MSAL.js frontend) | Raise `TokenExpiredError` → route handler returns 401 → MSAL.js refreshes token → frontend retries |
| App-only (from `get_access_token_app`) | MSAL backend cache handles refresh. If Graph still returns 401, it is almost certainly a missing admin consent issue, not token expiry. |

### 403 likely_cause hints (internal logs only, not shown to users)

| graph_code | Endpoint | Logged cause |
|-----------|----------|-------------|
| `Authorization_RequestDenied` | `.../transcripts/...` | Missing `OnlineMeetingTranscript.Read.All` permission, or transcription policy not enabled in M365 tenant |
| `Authorization_RequestDenied` | `.../recordings/...` | Missing `OnlineMeetingRecording.Read.All` permission |
| `Authorization_RequestDenied` | `.../onlineMeetings/...` | Missing `OnlineMeetings.Read` permission |
| `AccessDenied` / `Forbidden` | Any | Admin consent not granted in customer tenant |

### 404 likely_cause hints (internal logs only, not shown to users)

| Endpoint pattern | Logged cause |
|-----------------|-------------|
| `.../transcripts/{id}/content` | Transcript content not yet processed — retry in 5–10 min |
| `.../transcripts/` | Transcription not enabled, or transcript ID incorrect |
| `.../recordings/` | Meeting not recorded, or recording was deleted |
| `.../onlineMeetings/` | Meeting deleted, ID incorrect, or organiser account removed |
| `/users/` | User account deleted or ID/UPN incorrect |

### Verified transcript response shape (live call 2026-03-25)

```json
{
  "value": [
    {
      "id": "<transcript-id>",
      "meetingId": "...",
      "callId": "...",
      "contentCorrelationId": "...",
      "transcriptContentUrl": "...",
      "createdDateTime": "2026-03-25T10:31:24Z",
      "endDateTime": "2026-03-25T11:16:12Z",
      "meetingOrganizer": {
        "user": {
          "id": "<user-id>",
          "displayName": null,
          "tenantId": "..."
        }
      }
    }
  ]
}
```

`displayName` on `meetingOrganizer.user` is **always null** — same as meeting participants.  
Transcript content format: `text/vtt` (raw string). Parsing is `services/ingestion/vtt_parser.py`.

---

## 18. Open Questions — Do Not Implement Until Resolved

These decisions have NOT been finalized. Do not write code that depends on them.

| # | Question | Affects | Status |
|---|----------|---------|--------|
| 1 | ~~Does the backend use OBO (On-Behalf-Of) flow to call Graph API on behalf of users, or does the frontend pass a delegated Graph token directly?~~ | `graph/meetings.py`, `graph/transcripts.py`, ingest routes | ✅ **RESOLVED 2026-04-22** — Option C: app-only tokens for all Graph calls. Backend never calls Graph on behalf of a user. Ingestion is webhook-triggered (Celery, app token, `/users/{user_id}/` paths). Routes read from DB only, never call Graph directly. No OBO, no delegated Graph token from frontend. |
| 2 | Key Vault secret naming convention — confirmed as `db-{org_name}`? | `core/keyvault.py`, `provisioning/` | ⏳ Pending provisioning team |
| 3 | Database name on PostgreSQL server — is it `pg-{org_name}` or `{org_name}` or something else? | `db/manager.py`, connection string template | ⏳ Pending provisioning team |

---

## 18. What Not to Assume — Ever

- **Do not assume any Graph API endpoint URI** beyond those listed in Section 16.
  Verify new endpoints before using them.
- **Do not hardcode tenant IDs, client IDs, or secrets** anywhere in code.
  All config comes from `settings`.
- **Do not use `/me/` endpoints with an app token.** They return 400.
  App tokens must use `/users/{user_id}/` paths.
- **Do not validate `iss` as a hardcoded string.** It is per-tenant.
  Construct it from `tid` as shown in Section 8.
- **Do not read `displayName` from Graph meeting participant responses.** It is always
  null. Call `get_user_by_id(upn)` to get the real name.
- **Do not store secrets in any database column.** Credentials live in Key Vault only.
- **Do not skip SSL on tenant DB connections.** Always `sslmode=require`.
- **Do not pass `admin_user_id` as a plain request parameter.** Identity comes from
  JWT via `get_current_user` dependency only.
- **Do not call DB helpers directly from route handlers** without going through
  a service, unless the operation is a simple single-table lookup.
- **Do not trust `request.state` values** without going through `get_current_user`.
  Middleware does unverified decode only.
- **Do not modify files outside Scope A** (see Section 3).

---

*Prepared: 2026-04-21*  
*All values in this document were verified against live systems during architecture review.*  
*Update this file immediately when any open question is resolved.*
