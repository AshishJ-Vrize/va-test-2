# VRIZE Video Analytics — Production Requirements

> **v1.0 — Production** | VRIZE Inc — 2025  
> Technical & functional requirements for the production-grade Video Analytics platform. Replaces MVP stack with enterprise-grade technologies.

**Frontend:** Next.js 15 | **Backend:** FastAPI | **Infrastructure:** Azure | **Distribution:** Microsoft Teams

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Production Tech Stack](#2-production-tech-stack)
3. [Functional Requirements](#3-functional-requirements)
4. [Non-Functional Requirements](#4-non-functional-requirements)
5. [System Architecture](#5-system-architecture)
6. [Frontend Specification](#6-frontend-specification)
7. [Backend Specification](#7-backend-specification)
8. [Database Specification](#8-database-specification)
9. [Authentication & Authorization](#9-authentication--authorization)
10. [Infrastructure Specification](#10-infrastructure-specification)
11. [Microsoft Teams App](#11-microsoft-teams-app)
12. [Billing & License Management](#12-billing--license-management)
13. [CI/CD Pipeline](#13-cicd-pipeline)
14. [Development Roadmap](#14-development-roadmap)

---

## 1. Project Overview

VRIZE Video Analytics is a B2B SaaS product that integrates with Microsoft Teams to automatically capture, transcribe, and analyze meeting recordings using AI.

### Product Goal

Give enterprise teams AI-powered insights from every Teams meeting — summaries, action items, sentiment analysis, compliance alerts — without any manual effort.

### Target Users

| User | Use Case |
|------|----------|
| Team Managers | Meeting insights, performance |
| HR / Compliance | Rule violations, audit trail |
| Sales Teams | Call analytics, follow-ups |
| IT Admins | App management, user access |

### Business Model

- **Deployment model:** Dedicated deployment per enterprise client
- **Setup fee:** ₹1.5–3L one-time
- **Annual license:** ₹3–8L/year
- **Infrastructure:** Client pays their own Azure bill

---

### MVP Stack → Production Stack

| Layer | MVP (Current) | Production (This Document) | Reason |
|-------|--------------|---------------------------|--------|
| Frontend | Streamlit | Next.js 15 + React 19 | Real UI, Teams embed, mobile-ready |
| UI Components | Streamlit widgets | shadcn/ui + Tailwind v4 | Production-grade B2B components |
| Auth | MSAL device-code flow | NextAuth.js v5 + Entra ID | Real web OAuth2/PKCE, SSO |
| State Management | Streamlit session_state | TanStack Query v5 + Zustand | Caching, real-time sync |
| Background Jobs | threading.Thread | Celery + Redis | Reliable, monitored, scalable |
| Real-time Updates | Streamlit st.rerun() | Server-Sent Events (SSE) | Live meeting processing status |
| Backend | FastAPI (sync) | FastAPI (async) + Pydantic v2 | 3–5× throughput improvement |
| Database Driver | psycopg2 (sync) | asyncpg (async) | Non-blocking DB queries |
| Secrets | .env file | Azure Key Vault | Security, rotation, audit |
| Webhook Renewal | Manual endpoint | Celery beat (auto, every 2d) | Prevent silent data loss |
| Teams Distribution | None | Teams App + manifest | Native Teams sidebar presence |
| Licensing | None | VRIZE License Server | Subscription enforcement |

---

### Core Product Modules

| Module | Description |
|--------|-------------|
| **M1 — Meeting Ingestion** | Graph API webhooks capture call records. Transcripts fetched, chunked, embedded into pgvector. Status tracked per meeting. |
| **M2 — AI Insights** | GPT-4o generates: summary, action items, key decisions, risks, follow-ups, profanity detection. Per-meeting sentiment via Azure AI Language. |
| **M3 — RAG Chat** | Hybrid vector+keyword search across meeting transcripts. RBAC ensures users only query meetings they attended. |
| **M4 — Rules Engine** | 8 rule types: keyword, profanity threshold, speaker dominance, risk severity, missing action items, meeting duration, sentiment threshold, LLM freeform. |
| **M5 — Admin Panel** | RBAC roles (admin/user). Meeting access grants. User management. Violation acknowledgement. System health monitoring. |
| **M6 — Teams Integration** | Teams Tab in sidebar. SSO with Microsoft account. Webhook registration. App published via Teams Admin Center per client. |

---

## 2. Production Tech Stack

### Frontend Layer

| Technology | Package | Why | Replaces |
|------------|---------|-----|---------|
| **Next.js 15** | `next@15.0+` | App Router + React Server Components. Data-heavy dashboards benefit from RSC — fetch data on server, send HTML. No loading spinners for initial page. | Streamlit |
| **TypeScript 5 (strict)** | `typescript@5+` | Strict mode catches null/undefined bugs at compile time. B2B apps handle complex data shapes — type safety prevents runtime crashes. | Python type hints |
| **shadcn/ui + Tailwind v4** | `shadcn/ui`, `tailwindcss@4` | Copy-paste components, not an npm dependency. Full control over code. Tailwind v4 is 10× faster build. Charts, tables, dialogs — all production-ready. | Streamlit widgets |
| **TanStack Query v5** | `@tanstack/react-query@5` | Server state management: caching, background refetch, stale-while-revalidate, deduplication. Meeting data auto-refreshes without user action. | Streamlit st.cache_data |
| **Zustand v4** | `zustand@4` | Client UI state (sidebar, filters, selected meeting). Selective subscriptions prevent full re-renders on large dashboard datasets. | Streamlit session_state |
| **Recharts / Tremor** | `recharts@2`, `@tremor/react` | Sentiment timelines, speaker dominance charts, meeting frequency graphs. Tremor is purpose-built for B2B analytics dashboards. | Streamlit st.bar_chart |

### Backend Layer

| Technology | Package | Why | Replaces |
|------------|---------|-----|---------|
| **FastAPI 0.115+ (async)** | `fastapi@0.115+`, `uvicorn[standard]` | Fully async. Handles concurrent transcript processing + RAG queries without blocking. 3–5× throughput improvement over sync FastAPI. | Sync FastAPI (upgrade) |
| **Pydantic v2** | `pydantic@2.7+` | Rust-core validation engine. 10× faster than v1 for request/response validation. Critical for large transcript payloads. | pydantic v1 |
| **Celery + Redis** | `celery[redis]@5+`, `redis@5+` | Replaces threading.Thread. Insights, embedding, sentiment run as tasks. Beat scheduler auto-renews Graph webhooks every 2 days. Monitored via Flower. | threading.Thread worker |
| **Server-Sent Events** | FastAPI StreamingResponse | One-way push from server to browser. Meeting processing status streams in real-time without polling. Simpler than WebSockets. | Streamlit st.spinner |
| **asyncpg** | `asyncpg@0.29+`, `sqlalchemy[asyncio]` | Native async PostgreSQL driver. Works with async SQLAlchemy 2.0. Non-blocking DB queries — critical for RAG chatbot (3–4 DB calls per response). | psycopg2-binary |
| **Alembic 1.18+** | `alembic@1.18+` | Async-compatible migration support. New async env.py template with asyncpg connection. Runs in GitHub Actions before deploy. | Existing Alembic (upgrade) |

### AI & Azure Services

| Service | Package | Why |
|---------|---------|-----|
| **Azure OpenAI — GPT-4o** | `openai@1.0+` (Azure endpoint) | Insights generation: summary, action items, risks, decisions, profanity. Client's own Azure OpenAI endpoint — data stays in their tenant. |
| **Azure OpenAI — Embeddings** | `text-embedding-3-small` (1536-dim) | $0.02/1M tokens. Transcript chunks embedded for pgvector similarity search. No separate vector DB needed. |
| **Azure AI Language** | `azure-ai-textanalytics@5+` | Per-chunk sentiment scores (-1 to +1). Per-speaker sentiment breakdown. Free tier: 5,000 transactions/month. |
| **pgvector** | PostgreSQL extension | Stores 1536-dim embeddings in PostgreSQL. HNSW index for fast ANN search. No Pinecone/Weaviate needed — reduces architecture complexity. |
| **Microsoft Graph API** | httpx (REST calls) | CallRecords webhooks trigger on meeting end. Transcripts fetched via `/communications/callRecords`. Webhook renews every 2 days via Celery beat. |
| **Azure Blob Storage** | `azure-storage-blob@12+` | Raw VTT transcript files stored before processing. Meeting exports (PDF reports). $0.02/GB — negligible cost. |

### Infrastructure Layer

| Service | Package | Why |
|---------|---------|-----|
| **Azure Container Apps** | `az containerapp` | Serverless containers. Scale to zero overnight. Revision-based deployments enable blue/green zero-downtime updates. No Kubernetes overhead. |
| **Azure Container Registry** | `vrizeacr.azurecr.io` (VRIZE's) | Private Docker registry. All client deployments pull from VRIZE's ACR. VRIZE controls image versions. Revoke access anytime. Basic tier: $5/month. |
| **PostgreSQL 16 Flexible Server** | `azure postgres flexible-server` | Per-client dedicated instance (Option 3). B2ms for standard, D2s for high-volume. pgvector extension enabled. Point-in-time restore for DR. |
| **Azure Key Vault** | `azure-keyvault-secrets@4+` | All secrets: DB passwords, OpenAI keys, Graph credentials, license key. Replaces .env files in production. Managed Identity access. |
| **Azure Application Insights** | `azure-monitor-opentelemetry` | Distributed tracing, error tracking, performance monitoring. Native Azure integration. First 5GB/month free. |
| **GitHub Actions** | `.github/workflows/deploy.yml` | CI/CD: tests → Docker build → ACR push → migration → blue/green deploy. Parallel matrix deployment across all clients. |

---

## 3. Functional Requirements

Priority levels: **MUST** (launch blocker) | **SHOULD** (important) | **COULD** (nice to have)

### F1 — Meeting Ingestion

| ID | Requirement | Priority |
|----|-------------|----------|
| F1.1 | System must register a Microsoft Graph webhook for callRecords on deployment startup | **MUST** |
| F1.2 | Webhook subscription must auto-renew every 2 days via Celery beat (expires every 3 days) | **MUST** |
| F1.3 | System must fetch VTT transcript file for each completed meeting via Graph API | **MUST** |
| F1.4 | Transcript must be chunked by speaker turn and embedded via text-embedding-3-small | **MUST** |
| F1.5 | Ingestion status must be trackable: `pending → processing → embedded → done / failed` | **MUST** |
| F1.6 | Frontend must show real-time ingestion progress via Server-Sent Events | SHOULD |
| F1.7 | Admin must be able to manually trigger re-ingestion for a specific meeting | SHOULD |

### F2 — AI Insights

| ID | Requirement | Priority |
|----|-------------|----------|
| F2.1 | System must generate: meeting summary, action items, key decisions, risks & blockers, follow-ups | **MUST** |
| F2.2 | System must detect and log profanity with speaker name, timestamp, text, and severity | **MUST** |
| F2.3 | Per-chunk and per-meeting sentiment analysis must run via Azure AI Language | **MUST** |
| F2.4 | Per-speaker sentiment breakdown must be generated for each meeting | **MUST** |
| F2.5 | Insights must be generated asynchronously via Celery (not blocking API response) | **MUST** |
| F2.6 | Admin must be able to backfill insights for meetings processed before insights were added | SHOULD |
| F2.7 | System should track which GPT model version generated each insight row | SHOULD |

### F3 — RAG Chat

| ID | Requirement | Priority |
|----|-------------|----------|
| F3.1 | Users must be able to ask questions about meetings in natural language | **MUST** |
| F3.2 | System must enforce RBAC — users can only query meetings they participated in | **MUST** |
| F3.3 | RAG must use hybrid search (vector similarity + keyword) for better recall | **MUST** |
| F3.4 | Chat responses must include source attribution (meeting name, speaker, timestamp) | SHOULD |
| F3.5 | Chat history must persist within session and be resubmittable for follow-up questions | SHOULD |
| F3.6 | Chat interface must stream GPT response tokens in real-time (not wait for full completion) | COULD |

### F4 — Rules Engine

| ID | Requirement | Priority |
|----|-------------|----------|
| F4.1 | Admin must be able to create rules via natural language chat interface | **MUST** |
| F4.2 | System must support 8 rule types: `keyword`, `profanity_threshold`, `speaker_keyword`, `risk_severity`, `missing_action_items`, `speaker_dominance`, `meeting_duration`, `llm_freeform` | **MUST** |
| F4.3 | Violations must be created per (rule, meeting) pair with evidence details | **MUST** |
| F4.4 | All rule changes must be versioned with immutable history (who changed, when, why) | **MUST** |
| F4.5 | Admin must be able to acknowledge or dismiss violations with audit trail | **MUST** |
| F4.6 | System should send email/Teams notification when critical violation is detected | SHOULD |

### F5 — Auth & RBAC

| ID | Requirement | Priority |
|----|-------------|----------|
| F5.1 | Users must authenticate via Microsoft SSO (Entra ID) — no separate username/password | **MUST** |
| F5.2 | Two roles: `admin` (full access) and `user` (own meetings only) | **MUST** |
| F5.3 | Admin must be able to grant access to a specific meeting for a specific user | **MUST** |
| F5.4 | All API endpoints must validate JWT token on every request | **MUST** |
| F5.5 | Session must persist across browser refreshes via NextAuth.js session cookies | **MUST** |

### F6 — Teams App

| ID | Requirement | Priority |
|----|-------------|----------|
| F6.1 | App must be installable as a Teams Tab visible in the sidebar | **MUST** |
| F6.2 | SSO must work inside Teams — users should not be asked to log in again | **MUST** |
| F6.3 | App manifest must be distributable as a .zip for Teams Admin Center upload | **MUST** |
| F6.4 | App must be fully functional on Teams desktop, web, and mobile | SHOULD |
| F6.5 | App should be submittable to Microsoft AppSource for Phase 3 | COULD |

---

## 4. Non-Functional Requirements

### Performance

| Metric | Target |
|--------|--------|
| API response time (p95) | < 400ms |
| RAG chat first token | < 2 seconds |
| Page initial load | < 1.5 seconds (LCP) |
| Meeting ingestion (1hr meeting) | < 90 seconds total |
| Insights generation | < 45 seconds per meeting |
| Vector search (pgvector) | < 200ms for top-10 |
| Concurrent users | 50 per deployment |

### Security

| Requirement | Implementation |
|-------------|---------------|
| Auth on all endpoints | JWT middleware, Azure Entra ID |
| Secrets management | Azure Key Vault, no .env in prod |
| Transport security | HTTPS only, TLS 1.2+ enforced |
| Container security | Non-root user, read-only FS |
| Webhook validation | HMAC-SHA256 signature check |
| SQL injection prevention | SQLAlchemy ORM (parameterised) |
| Rate limiting | slowapi, 100 req/min per user |

### Reliability & Availability

| Requirement | Target |
|-------------|--------|
| Uptime SLA | 99.5% monthly |
| Deployment downtime | Zero (blue/green revisions) |
| DB backup frequency | Daily automated (Azure) |
| DB point-in-time restore | Up to 7 days |
| Webhook renewal | Automatic, every 2 days |
| Failed job retry | 3 retries with exponential backoff |
| License check offline | 7-day grace if server unreachable |

### Observability

| What | How |
|------|-----|
| API request tracing | Azure Application Insights |
| Error alerting | App Insights alerts → email |
| Background job status | Celery Flower dashboard |
| Health check endpoint | GET /api/v1/health (tests DB, Redis) |
| Structured logging | JSON logs → App Insights |
| License status | VRIZE admin dashboard |

### Scalability Targets

| Metric | Target |
|--------|--------|
| Concurrent users per deployment | 50 |
| Meetings stored per client | 500 |
| Vector embeddings per client | 100,000 |
| Client deployments managed | 30+ |

---

## 5. System Architecture

### Component Diagram

```
Client's Microsoft Teams (Desktop / Web / Mobile)
  │
  │  Teams App Tab (manifest.json → loads Next.js)
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CLIENT'S AZURE SUBSCRIPTION                      │
│                                                                     │
│  ┌─────────────────────────┐    ┌────────────────────────────────┐  │
│  │   Next.js 15 (UI)       │    │   FastAPI (API)                │  │
│  │   Container App         │◄──►│   Container App                │  │
│  │                         │    │                                │  │
│  │  • Dashboard            │    │  • /api/v1/meetings            │  │
│  │  • Chat interface       │    │  • /api/v1/chat                │  │
│  │  • Rules panel          │    │  • /api/v1/webhook/*           │  │
│  │  • Admin panel          │    │  • /api/v1/admin/*             │  │
│  │  • Violations view      │    │  • /api/v1/health              │  │
│  └─────────────────────────┘    └────────────┬───────────────────┘  │
│                                              │                      │
│  ┌──────────────────────┐    ┌───────────────▼──────────────────┐   │
│  │  Celery Workers      │    │   PostgreSQL 16 + pgvector       │   │
│  │  Container App       │◄──►│   Flexible Server (B2ms)         │   │
│  │                      │    │                                  │   │
│  │  • Insights task     │    │  • users, meetings, chunks       │   │
│  │  • Embedding task    │    │  • transcripts, insights         │   │
│  │  • Sentiment task    │    │  • rules, violations             │   │
│  │  • Webhook renewal   │    │  • 1536-dim embeddings           │   │
│  └──────────┬───────────┘    └──────────────────────────────────┘   │
│             │                                                        │
│  ┌──────────▼───────────┐    ┌──────────────────────────────────┐   │
│  │  Redis Cache         │    │   Azure Key Vault                │   │
│  │  • Celery broker     │    │   • DB credentials               │   │
│  │  • MSAL token cache  │    │   • OpenAI keys                  │   │
│  └──────────────────────┘    │   • Graph credentials            │   │
│                              │   • License key                  │   │
│  ┌──────────────────────┐    └──────────────────────────────────┘   │
│  │  App Insights        │                                           │
│  │  • Logs + traces     │    ┌──────────────────────────────────┐   │
│  │  • Error alerts      │    │   Blob Storage                   │   │
│  │  • Performance       │    │   • Raw VTT transcripts          │   │
│  └──────────────────────┘    │   • Exported reports             │   │
│                              └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
  Microsoft Graph API               VRIZE License Server
  (Teams webhooks,                  (license.vrize.com)
   transcripts, user info)          Checks subscription status

         │
         ▼
  Azure OpenAI (GPT-4o)       Azure AI Language
  text-embedding-3-small       (Sentiment analysis)
```

### Meeting Processing Flow

```
Meeting ends in Teams
    ↓
Graph webhook fires → POST /api/v1/webhook/call-records
    ↓
FastAPI validates webhook signature (HMAC-SHA256)
    ↓
Celery task queued: ingest_meeting(meeting_id)
    ↓
┌─── Celery Worker ──────────────────────────────────────┐
│  1. Fetch transcript VTT from Graph API                │
│  2. Store raw VTT in Azure Blob Storage                │
│  3. Parse VTT → speaker turns                          │
│  4. Chunk by speaker (300-500 token chunks)            │
│  5. Embed each chunk via text-embedding-3-small        │
│  6. Store chunks + embeddings in PostgreSQL            │
│  7. Queue insights task: generate_insights(meeting_id) │
│  8. Queue sentiment task: analyze_sentiment(meeting_id)│
└───────────────────────────────────────────────────────┘
    ↓
Insights task: GPT-4o generates summary, actions, etc.
Sentiment task: Azure AI Language scores each chunk
    ↓
Rules engine runs: check all active rules against meeting
    ↓
Violations created for any triggered rules
    ↓
Next.js dashboard updates via SSE stream
User sees: ✓ Meeting processed, insights ready
```

---

## 6. Frontend Specification

### Project Structure

```
video-analytics-ui/
├── app/                          # Next.js 15 App Router
│   ├── layout.tsx                # Root layout (Providers, Nav)
│   ├── page.tsx                  # Dashboard home (redirect to /meetings)
│   ├── meetings/
│   │   ├── page.tsx              # Meeting list + search
│   │   └── [id]/
│   │       ├── page.tsx          # Meeting detail (insights, transcript)
│   │       └── chat/page.tsx     # RAG chat for this meeting
│   ├── chat/page.tsx             # Global chat (all meetings)
│   ├── rules/
│   │   ├── page.tsx              # Rules list + violations
│   │   └── chat/page.tsx         # Rules chatbot
│   ├── admin/
│   │   ├── users/page.tsx        # User management
│   │   └── settings/page.tsx     # App settings
│   └── api/
│       └── auth/[...nextauth]/route.ts  # NextAuth handler
│
├── components/
│   ├── ui/                       # shadcn/ui components (copied in)
│   ├── meetings/                 # MeetingCard, MeetingTable, InsightPanel
│   ├── chat/                     # ChatMessage, ChatInput, SourceCitation
│   ├── rules/                    # RuleCard, ViolationBadge, RuleChatbot
│   ├── charts/                   # SentimentTimeline, SpeakerDonut
│   └── layout/                   # Sidebar, Header, BreadcrumbNav
│
├── lib/
│   ├── api.ts                    # API client (fetch wrappers)
│   ├── auth.ts                   # NextAuth config
│   └── utils.ts                  # cn(), formatDate(), etc.
│
├── hooks/
│   ├── useMeetings.ts            # TanStack Query hooks
│   ├── useInsights.ts
│   ├── useSSE.ts                 # Server-Sent Events hook
│   └── useRules.ts
│
└── store/
    ├── uiStore.ts                # Zustand: sidebar, filters, modals
    └── sessionStore.ts           # Zustand: current user, permissions
```

### Key Pages

| Route | Description |
|-------|-------------|
| `/meetings` | List of all meetings, search, filter by date/status |
| `/meetings/[id]` | Insights panel, transcript viewer, sentiment chart |
| `/meetings/[id]/chat` | RAG chat scoped to single meeting |
| `/chat` | Global RAG chat across all accessible meetings |
| `/rules` | Active rules list, violations table, acknowledge actions |
| `/rules/chat` | Natural language rule creation chatbot |
| `/admin/users` | User list, role assignment, meeting access grants |

### Key Components

| Component | Purpose |
|-----------|---------|
| `InsightPanel` | Tabbed view: Summary / Actions / Decisions / Risks |
| `SentimentTimeline` | Recharts line chart of sentiment over meeting duration |
| `SpeakerDonut` | Speaker talk-time distribution pie chart |
| `ChatMessage` | Message bubble with source citations expandable |
| `ViolationBadge` | Severity-coloured badge with acknowledge button |
| `ProcessingStatus` | SSE-powered live status: queued → done |

### State Management Pattern

```typescript
// Server state (meetings list, insights) → TanStack Query
const { data: meetings, isLoading } = useQuery({
  queryKey: ['meetings'],
  queryFn: () => api.getMeetings(),
  staleTime: 60_000,          // refresh every 60 seconds
  refetchOnWindowFocus: true, // update when user comes back to tab
})

// Real-time processing status → Server-Sent Events
const { status, progress } = useSSE(`/api/v1/meetings/${id}/stream`)
// status: "queued" | "embedding" | "insights" | "done" | "failed"

// UI state (sidebar, selected filters) → Zustand
const { sidebarOpen, activeFilter, setActiveFilter } = useUIStore()

// Auth state → NextAuth
const { data: session } = useSession()
const isAdmin = session?.user?.role === 'admin'
```

---

## 7. Backend Specification

### Project Structure

```
video-analytics-api/
├── app/
│   ├── main.py                   # FastAPI app, middleware, lifespan
│   ├── api/
│   │   ├── routes/
│   │   │   ├── meetings.py       # Meeting CRUD + sync
│   │   │   ├── ingest.py         # Ingestion trigger + status
│   │   │   ├── chat.py           # RAG chat + streaming
│   │   │   ├── rules.py          # Rules CRUD + violations
│   │   │   ├── admin.py          # User management, RBAC
│   │   │   ├── webhook.py        # Graph webhook receiver
│   │   │   └── health.py         # Deep health check
│   │   └── deps.py               # FastAPI dependencies (auth, db)
│   ├── auth/
│   │   ├── middleware.py         # JWT validation middleware
│   │   └── jwt.py                # Token decode, claims extraction
│   ├── config/settings.py        # Pydantic Settings (Key Vault aware)
│   ├── db/
│   │   ├── session.py            # Async SQLAlchemy engine + session
│   │   ├── models.py             # SQLAlchemy models
│   │   └── helpers/              # DB operations per domain
│   ├── services/                 # Business logic (unchanged from MVP)
│   └── tasks/
│       ├── celery_app.py         # Celery configuration
│       ├── ingestion.py          # ingest_meeting task
│       ├── insights.py           # generate_insights task
│       ├── sentiment.py          # analyze_sentiment task
│       └── webhook_renewal.py    # Celery beat: renew webhooks
└── alembic/                      # Database migrations
```

### API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/v1/meetings` | user+ | List meetings accessible to current user |
| POST | `/api/v1/meetings/sync` | user+ | Sync meetings from Graph API |
| GET | `/api/v1/meetings/{id}/stream` | user+ | SSE stream for processing status |
| POST | `/api/v1/ingest/meeting` | user+ | Trigger ingestion for a meeting |
| POST | `/api/v1/chat` | user+ | RAG chat query (RBAC enforced) |
| POST | `/api/v1/webhook/call-records` | Graph signature | Receive Graph change notifications |
| POST | `/api/v1/admin/rules/chat` | admin | Natural language rule creation |
| GET | `/api/v1/admin/violations` | admin | List violations with filters |
| POST | `/api/v1/admin/users/{id}/role` | admin | Set user role |
| GET | `/api/v1/health` | none | Deep health check (DB + Redis + config) |

### Celery Tasks

| Task | Trigger | Retry Policy | Est. Duration |
|------|---------|-------------|--------------|
| `ingest_meeting` | Webhook notification | 3× exponential backoff | 10–30 seconds |
| `generate_insights` | After embedding complete | 3× exponential backoff | 20–45 seconds |
| `analyze_sentiment` | After embedding complete | 3× exponential backoff | 5–15 seconds |
| `renew_webhooks` (beat) | Every 2 days (cron) | 3× immediate retry | 2–5 seconds |
| `apply_rules` (beat) | Every 6 hours (cron) | 1× retry | Varies by meeting count |

---

## 8. Database Specification

### Schema Overview (9 Tables)

```
users              — Microsoft Graph users with RBAC roles
meetings           — Teams meetings with metadata
meeting_participants — User ↔ Meeting many-to-many (access gate)
transcripts        — VTT transcript files per meeting
chunks             — Embedded transcript segments (1536-dim vector)
meeting_insights   — AI-generated insights per meeting
rules              — Compliance rules (current state)
rule_versions      — Immutable history of rule changes
rule_violations    — Flagged (rule, meeting) pairs with evidence
```

### pgvector Configuration

```sql
-- Enable extension (run once on new DB)
CREATE EXTENSION IF NOT EXISTS vector;

-- chunks table — stores 1536-dim embeddings
CREATE TABLE chunks (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  transcript_id  UUID REFERENCES transcripts(id) ON DELETE CASCADE,
  chunk_index    INTEGER,
  speaker_name   VARCHAR,
  spoken_text    TEXT NOT NULL,
  start_time     VARCHAR,
  embedding      vector(1536),         -- pgvector column
  status         VARCHAR DEFAULT 'pending',
  sentiment_score FLOAT,
  created_at     TIMESTAMP DEFAULT NOW()
);

-- HNSW index for fast approximate nearest-neighbor search
-- Build AFTER bulk insert (not before) for best performance
CREATE INDEX CONCURRENTLY chunks_embedding_idx
  ON chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Similarity search query (used in RAG)
SELECT spoken_text, speaker_name, start_time,
       1 - (embedding <=> $1::vector) AS similarity
FROM chunks
WHERE transcript_id IN (
  SELECT t.id FROM transcripts t
  JOIN meetings m ON t.meeting_id = m.id
  JOIN meeting_participants mp ON mp.meeting_id = m.id
  WHERE mp.user_id = $2  -- RBAC enforcement
)
ORDER BY embedding <=> $1::vector
LIMIT 10;
```

### Migration Rules (Zero-Downtime)

| Operation | Safe Approach | Why |
|-----------|--------------|-----|
| Add column | Add with DEFAULT value | Old app ignores new column |
| Rename column | Add new name, copy data, drop old (2 releases) | Old app still reads old name in Release 1 |
| Drop column | Remove from code first, then drop in next release | App must stop using it before DB removes it |
| Add NOT NULL | Add as nullable first, backfill, then add constraint | Existing rows have no value |
| Add index | `CREATE INDEX CONCURRENTLY` | Doesn't lock table during build |
| New table | Safe to add any time | Old app doesn't know about it |

---

## 9. Authentication & Authorization

### Auth Flow

```
User opens Teams Tab / Web App
    ↓
Next.js checks for session (NextAuth cookie)
    ↓
No session? → Redirect to Microsoft login
    ↓
Microsoft Entra ID (PKCE OAuth2 flow)
  → User enters their work Microsoft credentials
  → MFA if required by their company
  → Consent screen (first time only)
    ↓
Entra ID returns: access_token + id_token + refresh_token
    ↓
NextAuth stores session securely (encrypted cookie)
Session contains: { user: { id, name, email, role, tenantId } }
    ↓
User is redirected to app dashboard
    ↓
Every API call from Next.js includes:
  Authorization: Bearer {access_token}
    ↓
FastAPI JWT middleware validates token:
  1. Fetch Entra ID public keys (cached 1h)
  2. Verify signature
  3. Check expiry
  4. Extract: sub (user ID), tid (tenant), email
  5. Attach to request.state.user
    ↓
Route handler uses request.state.user.id for all DB queries
RBAC checked per endpoint (is_admin check)
```

### NextAuth Configuration

```typescript
// lib/auth.ts
import NextAuth from "next-auth"
import MicrosoftEntraID from "next-auth/providers/microsoft-entra-id"

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    MicrosoftEntraID({
      clientId: process.env.AZURE_AD_CLIENT_ID!,
      clientSecret: process.env.AZURE_AD_CLIENT_SECRET!,
      issuer: `https://login.microsoftonline.com/${
        process.env.AZURE_AD_TENANT_ID}/v2.0`,
    }),
  ],
  callbacks: {
    async jwt({ token, account }) {
      if (account) {
        token.accessToken = account.access_token
      }
      return token
    },
    async session({ session, token }) {
      session.accessToken = token.accessToken as string
      return session
    }
  }
})
```

### RBAC Matrix

| Action | user | admin |
|--------|------|-------|
| View own meetings | ✅ | ✅ |
| View all meetings | ❌ | ✅ |
| RAG chat (own meetings) | ✅ | ✅ |
| Create/edit rules | ❌ | ✅ |
| View violations | ❌ | ✅ |
| Manage users | ❌ | ✅ |
| Trigger backfill | ❌ | ✅ |
| Grant meeting access | ❌ | ✅ |

---

## 10. Infrastructure Specification

### Azure Resources per Client

| Resource | Type / SKU | Purpose | Cost/Month |
|----------|-----------|---------|-----------|
| Container Apps Environment | Consumption | Hosts API + UI + Workers | ~$0 (pay per use) |
| Container App — API | 0.5 vCPU, 1GB, min 1 replica | FastAPI backend | ~$15–25 |
| Container App — UI | 0.25 vCPU, 0.5GB, min 1 replica | Next.js frontend | ~$8–12 |
| Container App — Celery Worker | 0.5 vCPU, 1GB, min 1 replica | Background jobs | ~$10–15 |
| PostgreSQL Flexible Server | Standard_B2ms (2 vCore, 8GB) | Primary database | ~$65–70 |
| Azure Cache for Redis | Basic C0 (250MB) | Celery broker + token cache | ~$16 |
| Azure Key Vault | Standard | All secrets | ~$1–3 |
| Azure Blob Storage | LRS, Hot tier | Raw transcripts, exports | ~$3–5 |
| Application Insights | Pay-as-you-go | Logs, tracing, alerts | ~$0–5 (5GB free) |
| Azure OpenAI | GPT-4o + Embeddings | AI processing | ~$15–30 (usage) |
| Azure AI Language | F0 free tier | Sentiment analysis | ~$0 (5k tx free) |
| **Total per client** | | **(100 meetings/month)** | **~$133–181/month** |

> Client pays their own Azure bill. VRIZE cost is only the shared ACR + license server + staging.

### VRIZE Fixed Costs

| Resource | Cost/Month |
|----------|-----------|
| Azure Container Registry (Basic) | ~$5 |
| License Server (small Container App) | ~$10 |
| VRIZE staging/alpha environment | ~$50–80 |
| GitHub Actions (private repo) | ~$0–10 |
| **Total VRIZE fixed cost** | **~$65–105/month** |

### Revenue vs Cost

| Clients | VRIZE Cost | Revenue (₹5L/client) | Margin |
|---------|-----------|---------------------|--------|
| 1 client | ~$90/mo | ₹5L/year | ~85% |
| 5 clients | ~$100/mo | ₹25L/year | ~92% |
| 10 clients | ~$115/mo | ₹50L/year | ~95% |
| 30 clients | ~$150/mo | ₹1.5Cr/year | ~97% |

---

## 11. Microsoft Teams App

### manifest.json Structure

```json
{
  "manifestVersion": "1.16",
  "version": "1.0.0",
  "id": "{Azure AD App Registration Client ID}",

  "name": { "short": "Video Analytics", "full": "VRIZE Video Analytics" },
  "description": {
    "short": "AI insights from Teams meetings",
    "full": "Capture, transcribe and analyze Microsoft Teams meetings with AI"
  },

  "staticTabs": [
    {
      "entityId": "dashboard",
      "name": "Dashboard",
      "contentUrl": "https://{client-ui-url}",
      "scopes": ["personal"]
    }
  ],

  "webApplicationInfo": {
    "id": "{Azure AD App Registration Client ID}",
    "resource": "api://{client-ui-domain}/{client-id}"
  },

  "validDomains": ["{client-ui-domain}"],
  "permissions": ["identity"]
}
```

### Distribution Per Client

| Step | Who | Time |
|------|-----|------|
| Generate client-specific manifest.json | VRIZE | 2 min (scripted) |
| Package zip (manifest + 2 icons) | VRIZE | 1 min |
| Send zip to client IT admin | VRIZE | 1 email |
| Upload to Teams Admin Center | Client IT | 10 min (one-time) |
| Assign to user group | Client IT | 5 min (one-time) |
| App appears in all user sidebars | Automatic | Instant |

> **Key advantage:** Code updates deploy silently to the manifest URL. Client IT only needs to re-upload the zip if the manifest itself changes (new permissions, icon, URL).

### Teams JS SDK Integration

```typescript
// components/layout/TeamsProvider.tsx
"use client"
import * as teams from "@microsoft/teams-js"
import { useEffect } from "react"

export function TeamsProvider({ children }) {
  useEffect(() => {
    teams.app.initialize().then(() => {
      teams.app.getContext().then(ctx => {
        // ctx.user.tenant.id — validate matches expected tenant
        // ctx.app.theme — apply Teams light/dark theme
      })
    }).catch(() => {
      // Not running inside Teams — that's fine (web access still works)
    })
  }, [])
  return <>{children}</>
}
```

---

## 12. Billing & License Management

### Payment Methods

| Provider | Use Case | Supports |
|----------|----------|---------|
| Razorpay | Indian clients (INR) | UPI, netbanking, cards, EMI, wallets |
| Stripe | International clients (USD/EUR) | Cards, bank transfers, invoices |
| Manual Invoice | Enterprise (₹5L+ contracts) | NEFT/RTGS, purchase orders |

### Billing Events

| Event | Action | Grace Period |
|-------|--------|-------------|
| Payment success | Auto-renew license +365 days | N/A |
| Payment failed | Retry 3× over 7 days | 7 days |
| Subscription expired | Show renewal screen | 7 days |
| After grace period | App blocks all requests (HTTP 402) | None |
| Manual suspend | Immediate block | None |

### License Server Endpoints

| Endpoint | Called By | Description |
|----------|-----------|-------------|
| `GET /check?client_id=&license_key=` | Client app (every 24h) | Check if subscription is valid |
| `POST /admin/licenses` | VRIZE (on signup) | Create new client license |
| `POST /admin/licenses/{id}/renew` | VRIZE or Razorpay webhook | Extend subscription by N days |
| `POST /admin/licenses/{id}/suspend` | VRIZE (non-payment) | Immediate suspension |
| `GET /admin/licenses` | VRIZE dashboard | View all clients + expiry status |

---

## 13. CI/CD Pipeline

### Pipeline Flow

```
git push origin main
    ↓
Job 1: TEST (2 min)
  └── pytest (FastAPI backend)
  └── TypeScript tsc --noEmit (Next.js)
  └── ESLint check
  └── If any fail → STOP, nothing deploys
    ↓
Job 2: BUILD (4 min)
  └── Build va-api:sha-{commit} Docker image
  └── Build va-ui:sha-{commit} Docker image
  └── Push both to vrizeacr.azurecr.io
    ↓
Job 3: DEPLOY (parallel, one job per active client in deploy/clients.json)

  Step A — Alembic migration
    └── Read DB creds from client's Key Vault
    └── alembic upgrade head (backwards-compatible migration)
    └── Old app version still running, no interruption

  Step B — Deploy new revision (0% traffic)
    └── az containerapp update --image va-api:sha-{commit}
    └── New revision starts, old revision keeps all traffic

  Step C — Health check (2 min timeout)
    └── Poll GET /api/v1/health every 5 seconds
    └── Returns 200 with DB + config checks → PASS
    └── Timeout → FAIL → old version keeps running

  Step D — Traffic shift (only if Step C passed)
    └── az containerapp ingress traffic set --revision-weight new=100
    └── Users seamlessly moved to new version

  Step E — Deploy UI
    └── az containerapp update --image va-ui:sha-{commit}

  On failure → Slack/email alert, old version unaffected
```

> `fail-fast: false` in the matrix strategy means one client deployment failure does NOT block other clients.

### GitHub Secrets Required

| Secret Name | Value | Where to Get It |
|-------------|-------|----------------|
| `ACR_USERNAME` | vrizeacr admin username | Azure Portal → ACR → Access Keys |
| `ACR_PASSWORD` | vrizeacr admin password | Azure Portal → ACR → Access Keys |
| `AZURE_CREDENTIALS` | Service principal JSON | `az ad sp create-for-rbac --sdk-auth` |

---

## 14. Development Roadmap

### Phase 1 — Production Foundation (6–8 weeks)
**Goal: Make the MVP production-safe**

**Week 1–2: Security**
- JWT middleware on all endpoints
- Azure Key Vault integration
- Replace .env in production
- Rate limiting (slowapi)

**Week 3–4: Reliability**
- Replace threading → Celery
- Webhook auto-renewal job
- Enhanced health check
- Dockerfiles + CI/CD pipeline

**Week 5–8: Deployment**
- Client deploy script (`deploy/setup_client.sh`)
- License server (`license_server/`)
- Teams app manifest
- Alpha environment on VRIZE Azure

---

### Phase 2 — Next.js Frontend (6–8 weeks)
**Goal: Replace Streamlit with production Next.js UI**

**Week 1–2: Setup**
- Next.js 15 project init
- NextAuth.js + Entra ID
- shadcn/ui + Tailwind v4
- TanStack Query + Zustand

**Week 3–5: Core Pages**
- Dashboard + Meeting list
- Meeting detail + insights
- RAG chat interface
- SSE processing status

**Week 6–8: Admin**
- Rules + violations panel
- Admin user management
- Sentiment charts (Recharts)
- Teams SDK integration

---

### Phase 3 — Scale & Monetise (Ongoing)
**Goal: First paid clients + AppSource submission**

**First Clients**
- Razorpay billing integration
- Automated license renewal
- Client onboarding docs
- Deploy first 3 paid clients

**AppSource**
- Microsoft Partner Center registration
- Multi-tenant app registration
- Privacy policy + ToS pages
- Security review submission

**Option B (Future — Split AI Architecture)**
- Thin client agent on client Azure
- VRIZE-hosted AI engine
- Per-API-call billing
- Usage metering dashboard

---

*Document version: 1.0 | Last updated: 2025 | VRIZE Inc*
