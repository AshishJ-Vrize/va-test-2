# Ingestion Service — Implementation Documentation

**Branch:** `ingestion-service`  
**Author:** Rahul Patel  
**Date:** April 2026  
**Scope:** Ingestion team — `app/services/ingestion/` and `app/api/routes/ingest.py`

---

## Table of Contents

1. [Overview — What the Ingestion Service Does](#1-overview)
2. [Files Created](#2-files-created)
3. [How the Pieces Connect](#3-how-the-pieces-connect)
4. [File-by-File Breakdown](#4-file-by-file-breakdown)
   - [vtt_parser.py](#41-vtt_parserpy)
   - [chunker.py](#42-chunkerpy)
   - [embedder.py](#43-embedderpy)
   - [pipeline.py](#44-pipelinepy)
   - [ingest.py (route)](#45-ingestpy--the-api-route)
5. [Database Tables Written To](#5-database-tables-written-to)
6. [Key Constants & Why They Are What They Are](#6-key-constants--why-they-are-what-they-are)
7. [Error Handling Reference](#7-error-handling-reference)
8. [Rules Every Developer Must Know](#8-rules-every-developer-must-know)
9. [Tests](#9-tests)
10. [Open Questions](#10-open-questions)

---

## 1. Overview

The ingestion service is responsible for taking a Microsoft Teams meeting and turning it into searchable, AI-ready data stored in the tenant database.

**Input:** A Teams meeting join URL + a Microsoft Graph token  
**Output:** Parsed transcript, text chunks with 1536-dimension embeddings, speaker analytics, credit usage record — all stored in the tenant PostgreSQL database

The service runs in two contexts:
- **Manual trigger:** User hits `POST /ingest/meeting` from the frontend
- **Webhook trigger:** Celery task calls `run_ingestion_pipeline()` directly after a webhook event (out of scope for this PR — handled by the workers team)

---

## 2. Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `app/services/ingestion/vtt_parser.py` | 146 | Parses raw Teams VTT transcript files |
| `app/services/ingestion/chunker.py` | 184 | Merges speaker turns, splits into chunks |
| `app/services/ingestion/embedder.py` | 132 | Embeds chunk text via Azure OpenAI |
| `app/services/ingestion/pipeline.py` | 225 | Orchestrates all steps, writes to DB |
| `app/api/routes/ingest.py` | 295 | `POST /ingest/meeting` API route handler |
| `tests/test_vtt_parser.py` | 246 | 25 unit tests for the VTT parser |
| `tests/test_chunker.py` | 246 | 27 unit tests for merge + chunk logic |
| `tests/test_ingestion.py` | 395 | 25 unit tests for pipeline + embedder |
| `tests/conftest.py` | 49 | Shared pytest fixture for env vars |

**Total: 77 tests, all passing. No real Azure or DB connections required.**

---

## 3. How the Pieces Connect

```
Frontend (MSAL.js)
    │
    │  POST /ingest/meeting
    │  { join_url, graph_token }
    ▼
app/api/routes/ingest.py          ← YOU ARE HERE (the entry point)
    │
    ├─ GraphClient(graph_token)
    │       │
    │       ├─ get_meeting_by_join_url()   → meeting metadata
    │       ├─ get_user_by_id()            → organizer display name
    │       ├─ get_transcripts()           → transcript list (may be empty)
    │       └─ get_transcript_content()    → raw VTT string
    │
    ├─ Upsert: User, Meeting, MeetingParticipant  (tenant DB)
    │
    ├─ CreditPricing lookup               (central DB)
    │
    └─ run_ingestion_pipeline()
            │
            ├─ vtt_parser.parse_vtt()       → list[VttSegment]
            ├─ chunker.merge_speaker_turns() → list[VttSegment]  (merged)
            ├─ chunker.chunk_segments()     → list[Chunk]
            ├─ embedder.embed_batch()       → list[list[float]]
            └─ DB writes:
                   Transcript row
                   Chunk rows (with embeddings)
                   SpeakerAnalytic rows
                   CreditUsage row
                   meeting.status = "ready"

    └─ db.commit()   ← ONLY DONE HERE, never inside the pipeline
```

---

## 4. File-by-File Breakdown

---

### 4.1 `vtt_parser.py`

**Job:** Convert the raw `.vtt` string from Microsoft Graph into structured Python objects.

#### Data Model

```python
@dataclass
class VttSegment:
    speaker:  str  # "Unknown" if no <v> tag — never empty
    text:     str  # cleaned spoken text — never empty
    start_ms: int  # milliseconds from meeting start
    end_ms:   int  # milliseconds from meeting start
```

#### Functions

**`_ts_to_ms(ts: str) -> int`**

Converts a VTT timestamp to milliseconds. Handles two formats Teams produces:

| Format | Example | When used |
|--------|---------|-----------|
| `HH:MM:SS.mmm` | `01:23:45.678` | Meetings longer than 1 hour |
| `MM:SS.mmm` | `23:45.678` | Short meetings (Teams default) |

```python
# How it works:
parts = "01:23:45.678".split(":")   # → ["01", "23", "45.678"]
# 3 parts → HH:MM:SS
# 2 parts → MM:SS (hours = 0)
milliseconds = (hours * 3600 + minutes * 60 + seconds) * 1000
```

**`parse_vtt(content: str) -> list[VttSegment]`**

Step-by-step:

1. **Normalise line endings** — replaces `\r\n` (Windows) and `\r` (old Mac) with `\n`
2. **Split into blocks** — VTT cue blocks are separated by blank lines
3. **Skip non-cue blocks** — ignores `WEBVTT`, `NOTE`, `STYLE`, `REGION` headers
4. **Find the timestamp line** — searches each block for a line matching `HH:MM:SS.mmm --> HH:MM:SS.mmm`. Skips past cue ID numbers (Teams adds `1`, `2`, `3`... before the timestamp).
5. **Extract speaker** — looks for `<v Speaker Name>` on the first text line. Falls back to `"Unknown"` if absent.
6. **Strip all tags** — removes `<b>`, `<i>`, `<00:00:01.000>`, and any other HTML/VTT inline tags.
7. **Discard empty cues** — if the text is blank after stripping, the cue is thrown away.
8. **Return ordered list** of `VttSegment` objects.

#### What It Handles

| Scenario | How handled |
|----------|-------------|
| `HH:MM:SS.mmm` timestamps | Native support |
| `MM:SS.mmm` timestamps | Inserts `hours=0` |
| Cue ID numbers (1, 2, 3...) before timestamp | Skipped — searches all lines for the timestamp pattern |
| `<v John Doe>text` speaker tag | Extracts "John Doe" |
| No `<v>` tag | Speaker = `"Unknown"` |
| Multi-line cue text | Lines joined with a space |
| `<b>bold</b>`, `<i>italic</i>` inline tags | Stripped |
| `<00:00:01.000>` timing tags | Stripped |
| CRLF line endings | Normalised to LF |
| NOTE / STYLE / REGION blocks | Skipped entirely |
| Cues with no text after stripping | Discarded |

---

### 4.2 `chunker.py`

**Job:** Turn the list of `VttSegment` objects into right-sized `Chunk` objects for embedding.

#### Constants

```python
MAX_WORDS_PER_CHUNK = 300
# Why 300: text-embedding-3-small has an 8,191 token limit.
# 300 words ≈ 400 tokens — stays well within the limit with headroom for
# any tokenisation overhead.

MERGE_GAP_MS = 2_000  # 2 seconds
# Why 2s: covers natural speaker pauses without merging turns that are
# truly separate thoughts.
```

#### Data Model

```python
@dataclass
class Chunk:
    chunk_index: int  # 0-based, globally sequential across the whole meeting
    text:        str  # spoken content — never empty
    speaker:     str  # display name — never empty
    start_ms:    int  # meeting-relative start time in milliseconds
    end_ms:      int  # always strictly > start_ms
```

#### Functions

**`merge_speaker_turns(segments: list[VttSegment]) -> list[VttSegment]`**

Teams splits a continuous speaker turn into many small cue blocks (often one per sentence). This function joins them back into natural thought units before chunking.

**Merge rules:**
- Same speaker AND gap ≤ 2000ms → merge into one segment
- Different speaker → never merge, even if they overlap in time
- Overlapping timestamps (negative gap) → treated as gap=0, always merged if same speaker
- When merged: `start_ms` from the first segment, `end_ms` from the last

```
Before merge:                          After merge:
John: "Hello" (0–2000ms)   ──┐
John: "world" (2500–4000ms) ──┘ gap=500ms ≤ 2000ms  →  John: "Hello world" (0–4000ms)

Jane: "Hi" (4100–5000ms)          →  Jane: "Hi" (4100–5000ms)   [different speaker]

John: "later" (8000–9000ms)       →  John: "later" (8000–9000ms) [gap=3000ms > 2000ms]
```

**Does not mutate the input list** — returns a new list.

---

**`chunk_segments(segments: list[VttSegment]) -> list[Chunk]`**

Splits each merged segment into chunks that fit within the 300-word limit.

**Splitting strategy:**

```
≤ 300 words → 1 chunk (segment used as-is)

> 300 words → split into sub-chunks of ≤ 300 words each
              timestamps distributed proportionally by word position:

Example: John speaks 600 words, 0ms → 60,000ms
  Chunk 0: words   1–300  →  start=0ms,      end=30,000ms   (50% through)
  Chunk 1: words 301–600  →  start=30,000ms, end=60,000ms
```

**Safety guard:** If `end_ms <= start_ms` (can happen with zero-duration VTT timestamps), `end_ms` is set to `start_ms + 1`. This ensures the DB constraint is always satisfied.

**`chunk_index` is global** — it counts up from 0 across all segments in the meeting, not per-speaker. So if John produces chunks 0, 1, 2 and then Jane produces chunk 3 — that ordering is preserved.

---

### 4.3 `embedder.py`

**Job:** Send chunk text to Azure OpenAI and get back a 1536-dimension vector for each chunk.

#### Constants

```python
EMBEDDING_DIM = 1536
# Must match Vector(1536) in app/db/tenant/models.py → Chunk.embedding
# text-embedding-3-small always outputs exactly 1536 floats

_BATCH_SIZE = 16
# Azure OpenAI enforces a maximum of 16 inputs per embeddings API call
```

#### The Singleton Client

```python
_client: AzureOpenAI | None = None  # module-level

def _get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        _client = AzureOpenAI(...)  # created once
    return _client
```

**Why a singleton:** Creating a new `AzureOpenAI` instance per call creates a new HTTP connection pool per call. That wastes memory and adds latency. The singleton keeps one pool alive for the lifetime of the worker process.

#### Functions

**`embed_single(text: str) -> list[float]`**

Convenience wrapper. Returns one 1536-dim vector for a single text string.  
Used at query time (not during ingestion). Internally calls `embed_batch([text])[0]`.

---

**`embed_batch(texts: list[str]) -> list[list[float]]`**

Main batching function. Takes any number of texts and returns one vector per text, in the same order.

```
texts = ["chunk 0 text", "chunk 1 text", ..., "chunk 16 text", "chunk 17 text"]
                                                               ↑
                                              16 per call limit reached here

API call 1:  texts[0:16]   → 16 vectors
API call 2:  texts[16:17]  →  1 vector

return: 17 vectors in input order
```

Empty input (`[]`) returns `[]` immediately without an API call.

---

**`_embed_sub_batch(texts: list[str]) -> list[list[float]]`**  *(internal — do not call directly)*

Calls the Azure OpenAI API for one sub-batch of ≤ 16 texts.

Decorated with `@retry` from Tenacity:

```
Retries on: HTTP 429 RateLimitError only
Strategy:   Exponential backoff — waits 2s, 4s, 8s, 16s, 32s between attempts
Max wait:   60 seconds per wait period
Max tries:  5 attempts (~2 minutes total maximum wait)
After 5:    Re-raises the original RateLimitError
```

Other errors (400, network errors, etc.) are **not retried** — they raise immediately because they represent code bugs or config problems that won't resolve on retry.

**Dimension validation:** Every vector is checked to be exactly 1536 floats. If it's not, `ValueError` is raised immediately (not retried). This means the deployment name is wrong — fix `AZURE_OPENAI_DEPLOYMENT_EMBEDDING` in your `.env`.

---

### 4.4 `pipeline.py`

**Job:** The orchestrator. Calls all three service files in order and writes the results to the tenant database.

**Signature:**
```python
def run_ingestion_pipeline(
    meeting_id: uuid.UUID,    # UUID of the meetings row — must already exist
    vtt_content: str,         # raw VTT string from Graph API
    db: Session,              # active SQLAlchemy Session (tenant DB)
    credits_per_minute: int,  # from central DB CreditPricing for this tenant's plan
) -> None
```

#### The 10 Steps

| Step | What happens | DB action |
|------|-------------|-----------|
| **1** | `meeting.status = "ingesting"` — signals to other services that this meeting is being processed | `flush()` |
| **2** | `parse_vtt(vtt_content)` — turns the raw VTT string into `list[VttSegment]`. Raises `ValueError` if result is empty. | — |
| **3** | Count total words, set `language = "en"` (Teams transcripts are always English; multilingual support is future scope) | — |
| **4** | **Upsert Transcript row** — if a `Transcript` row already exists for this `meeting_id`, overwrite `raw_text`, `language`, `word_count`. If not, create a new row. | `flush()` to get `transcript.id` |
| **5** | `merge_speaker_turns()` + `chunk_segments()` — produces ordered list of `Chunk` objects | — |
| **6** | `embed_batch([c.text for c in chunks])` — sends all chunk texts to Azure OpenAI. Returns one 1536-dim vector per chunk. Retries on 429. | — |
| **7** | **Delete old chunks** for this transcript, then **insert new chunks** — each with `transcript_id`, `chunk_index`, `text`, `speaker`, `start_ms`, `end_ms`, `embedding` | `flush()` |
| **8** | Compute per-speaker stats from **raw segments** (not merged), **delete old SpeakerAnalytic rows**, insert new ones — each with `meeting_id`, `speaker_label`, `talk_time_seconds`, `word_count`. `user_id` is set to `None` (filled later by the diarization service). | `flush()` |
| **9** | Append a **CreditUsage** row — `credits_consumed = duration_minutes × credits_per_minute`. Falls back to 1 minute if `duration_minutes` is NULL. This table is append-only — never update or delete rows. | `flush()` |
| **10** | `meeting.status = "ready"` | `flush()` |

#### Critical Rule — No `db.commit()`

```python
# The pipeline only calls db.flush() — NEVER db.commit()
# The route handler (ingest.py) owns the transaction.
# This means:
#   - If the pipeline fails halfway, the caller can rollback the whole transaction
#   - The route decides WHEN to commit — after the pipeline succeeds
```

#### On Any Failure

```python
except Exception:
    meeting.status = "failed"
    db.flush()  # mark the meeting as failed
    log.exception("Ingestion failed for meeting %s", meeting_id)
    raise       # re-raise so the caller can commit the "failed" status
```

The pipeline sets `meeting.status = "failed"` and re-raises. The route handler then calls `db.commit()` to persist that failed status so the frontend can show an error.

#### Idempotent — Safe to Re-run

Re-ingesting the same meeting is safe:
- **Transcript:** Updated in place (no duplicate)
- **Chunks:** Old rows deleted, new rows inserted fresh
- **SpeakerAnalytic:** Old rows deleted, new rows inserted fresh
- **CreditUsage:** A new row is appended — this is intentional (audit trail of every ingestion attempt)

#### Why Speaker Analytics Use Raw Segments

Speaker stats are computed from the **raw (pre-merge)** segments, not the merged chunks. This is important because:

```
Raw:    John (0–2000ms) + John (2500–4000ms)  → merged into one segment John (0–4000ms)

Talk time from merged: 4000ms  ← WRONG (includes 500ms gap where nobody spoke)
Talk time from raw:    2000ms + 1500ms = 3500ms  ← CORRECT
```

---

### 4.5 `ingest.py` — The API Route

**Endpoint:** `POST /ingest/meeting`  
**Auth:** Required — `get_current_user` + `get_tenant_db` + `get_central_db`  
**Tag:** `ingestion`

#### Request Body

```python
{
    "join_url": "https://teams.microsoft.com/l/meetup-join/...",
    "graph_token": "eyJ0eXAiOiJKV1QiLCJhbGc..."
}
```

`graph_token` is a **delegated** Microsoft Graph token obtained by the frontend via MSAL.js. It must have these scopes: `User.Read`, `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All`.

> **Note:** CONTEXT.md Open Question #1 asks whether to use OBO (On-Behalf-Of) flow instead of a frontend-passed token. This is unresolved. When it's settled, only the `GraphClient` instantiation line in this file changes — nothing else in the route.

#### Response Body

```python
{
    "meeting_id": "550e8400-e29b-41d4-a716-446655440000",  # internal UUID
    "meeting_graph_id": "MSo1N2Y5OGIx...",                 # Graph meeting ID
    "status": "ready",                                      # or "pending" on 202
    "message": "Meeting transcript ingested successfully."
}
```

#### The 8 Steps in Detail

---

**Step 1 — Fetch meeting from Graph**

```python
gc = GraphClient(body.graph_token)
gm = gc.get_meeting_by_join_url(body.join_url)
```

Calls `GET /me/onlineMeetings?$filter=joinWebUrl eq '{join_url}'`.  
Returns the first match (join URLs are unique). Raises `MeetingNotFoundError` if no match.

Extracted from the Graph response:
- `meeting_graph_id` = `gm["id"]`
- `subject` = `gm.get("subject")` — falls back to `"Untitled Meeting"` if missing
- `start_dt` / `end_dt` — parsed from ISO 8601 strings (both `Z` and `+00:00` formats handled)
- `duration_minutes` — computed as `ceil((end - start).total_seconds() / 60)`, minimum 1

---

**Step 2 — Resolve organizer display name**

```python
organizer_graph_id = gm["participants"]["organizer"]["identity"]["user"]["id"]
organizer_upn      = gm["participants"]["organizer"]["upn"]

organizer_profile = gc.get_user_by_id(organizer_upn or organizer_graph_id)
```

**Why this call is needed:** Microsoft Graph always returns `displayName: null` in meeting participant responses. This is a confirmed Graph API behaviour. The only way to get the real display name is a separate `GET /users/{id}` call.

This call is best-effort — if it fails (token error, user deleted), the route falls back to using the UPN as the display name. The ingestion continues; it doesn't fail.

---

**Step 3 — Upsert users, meeting, participants**

Three upsert operations in sequence, each followed by a `db.flush()` to get the auto-generated UUID before it's needed as a foreign key:

```
1. _upsert_user(organizer)    → flush → organizer_user.id available
2. _upsert_meeting(...)       → flush → meeting_row.id available
3. _upsert_participant(organizer, role="organizer")
4. For each attendee in gm["participants"]["attendees"]:
       _upsert_user(attendee)    → flush
       _upsert_participant(attendee, role="attendee")
5. flush()
```

**Upsert behaviour:**
- `_upsert_user`: queries by `graph_id` (the stable Azure AD object ID). If found, updates `email` and `display_name`. If not, inserts. Email and display_name are never left empty.
- `_upsert_meeting`: queries by `meeting_graph_id` (the dedup key). If found, updates all mutable metadata fields but **preserves `status`** (the pipeline will set it). If not, inserts with `status="pending"`.
- `_upsert_participant`: inserts `(meeting_id, user_id, role)` only if the pair doesn't already exist. No-op on re-ingestion.

**Attendees note:** For attendees, the route uses the `graph_id` and `upn` from the meeting response directly (no additional `get_user_by_id` call). This avoids N+1 Graph API calls for large meetings. Display names for attendees can be backfilled later by a background task.

---

**Step 4 — Check transcript availability**

```python
transcripts = gc.get_transcripts(meeting_graph_id)
```

Calls `GET /me/onlineMeetings/{meeting_graph_id}/transcripts`.

**If the list is empty:**

```
→ commit the meeting + participant rows
→ return HTTP 202 Accepted
```

Teams takes **5–10 minutes** after a meeting ends to process the transcript. An empty list is not an error — it means "not ready yet". The frontend should retry the same request later.

The 202 response body tells the user:
```json
{
  "status": "pending",
  "message": "Meeting saved. Transcript is not yet available..."
}
```

---

**Step 5 — Fetch the VTT content**

```python
vtt_content = gc.get_transcript_content(meeting_graph_id, transcripts[0]["id"])
```

Calls `GET .../transcripts/{transcript_id}/content?$format=text/vtt`.  
Timeout is **60 seconds** (not the standard 30s) because VTT files from long meetings can be several MB.

Returns the raw VTT string — no parsing done here. Parsing is the pipeline's job.

---

**Step 6 — Look up credit pricing**

```python
pricing = central_db.query(CreditPricing).filter(
    CreditPricing.plan == current_user.tenant.plan
).first()
credits_per_minute = pricing.credits_per_minute if pricing else 1
```

Reads `CreditPricing` from the **central DB** (not the tenant DB).  
Falls back to `1 credit/minute` if no pricing row exists — prevents a hard failure during schema bootstrap.

Plans: `trial` / `starter` / `pro` / `enterprise`

---

**Step 7 — Run the ingestion pipeline**

```python
run_ingestion_pipeline(
    meeting_id=meeting_row.id,
    vtt_content=vtt_content,
    db=tenant_db,
    credits_per_minute=credits_per_minute,
)
```

The pipeline does all the heavy work (parse → chunk → embed → persist). It never commits. If it raises, the meeting status is already set to `"failed"` inside the pipeline — the route catches the exception, commits the failed status, and returns an error response.

---

**Step 8 — Commit and return**

```python
tenant_db.commit()
return IngestMeetingResponse(meeting_id=..., status="ready", message="...")
```

This is the only `db.commit()` in the entire ingestion flow. The route owns the transaction.

---

#### Error Handling

| Error | HTTP | When it happens |
|-------|------|-----------------|
| `TokenExpiredError` | **401** | Graph token expired — user must re-authenticate via MSAL.js |
| `MeetingNotFoundError` | **404** | No meeting at this join URL in Graph |
| `GraphClientError` | **503** | Graph API returned 5xx, timed out, or rate limit exhausted |
| `ValueError` from pipeline | **422** | VTT has zero segments (blank or malformed transcript) |
| Any other pipeline exception | **503** | Embedding failure, DB error, etc. |
| No transcript in Graph | **202** | Teams still processing — not an error, retry later |

In all error cases where the pipeline ran (even partially), `db.commit()` is called before raising the `HTTPException` to persist the `"failed"` status on the meeting row.

---

## 5. Database Tables Written To

All tables below are in the **tenant database** (one per client). The `CreditPricing` lookup is from the **central database**.

| Table | Operation | Notes |
|-------|-----------|-------|
| `users` | Upsert | One row per Graph user ID. `email` and `display_name` updated on re-ingestion. |
| `meetings` | Upsert | `meeting_graph_id` is the unique dedup key. `status` transitions: `pending → ingesting → ready \| failed`. |
| `meeting_participants` | Insert (idempotent) | `(meeting_id, user_id)` composite PK prevents duplicates. |
| `transcripts` | Upsert | One per meeting (`meeting_id` UNIQUE). `raw_text`, `language`, `word_count` all populated. |
| `chunks` | Delete + Insert | Old chunks deleted before new ones are inserted. Each chunk has `embedding` (Vector 1536). |
| `speaker_analytics` | Delete + Insert | Old rows deleted before new ones are inserted. `user_id` is NULL (filled by diarization service later). |
| `credit_usage` | Insert (append-only) | **Never update or delete.** New row appended on every ingestion, including re-ingestions. |

---

## 6. Key Constants & Why They Are What They Are

| Constant | Value | Location | Reason |
|----------|-------|----------|--------|
| `MAX_WORDS_PER_CHUNK` | `300` | `chunker.py` | 300 words ≈ 400 tokens, well within text-embedding-3-small's 8,191-token limit |
| `MERGE_GAP_MS` | `2000` | `chunker.py` | 2 seconds covers natural pauses without merging truly separate thoughts |
| `EMBEDDING_DIM` | `1536` | `embedder.py` | text-embedding-3-small always outputs exactly 1536 floats. Must match `Vector(1536)` in DB models. |
| `_BATCH_SIZE` | `16` | `embedder.py` | Azure OpenAI enforces 16 inputs max per embeddings API call |
| Max retry attempts | `5` | `embedder.py` | 5 attempts on 429 = ~2 minutes max wait. After that, raise and mark meeting failed. |
| VTT fetch timeout | `60s` | `transcripts.py` | VTT files from long meetings can be several MB — larger than the standard 30s timeout |

---

## 7. Error Handling Reference

### Graph API Errors (in the route handler)

| Graph Error | Maps to | Description |
|-------------|---------|-------------|
| `TokenExpiredError` | HTTP 401 | Delegated token expired. Frontend re-auths via MSAL. |
| `MeetingNotFoundError` | HTTP 404 | No meeting at this join URL. |
| `GraphClientError` (any) | HTTP 503 | Network issue, 5xx from Graph, rate limit exhausted after retries. |

### Pipeline Errors (caught in the route after `run_ingestion_pipeline()`)

| Exception | Maps to | Description |
|-----------|---------|-------------|
| `ValueError` | HTTP 422 | VTT produced zero segments (empty or malformed transcript). |
| Any `Exception` | HTTP 503 | Embedding failure, DB error, or any other internal error. |

In all pipeline failure cases, the pipeline has already set `meeting.status = "failed"` before re-raising. The route commits this status before returning the error.

### Embedder Retry (inside the pipeline)

| Error | Behaviour |
|-------|-----------|
| `RateLimitError` (429) | Retry up to 5 times with exponential backoff (2–60s) |
| All other errors | Raise immediately — no retry |

---

## 8. Rules Every Developer Must Know

### 1. Never call `db.commit()` inside the pipeline

`run_ingestion_pipeline()` only calls `db.flush()`. The route handler calls `db.commit()`. Breaking this rule means partial data gets committed mid-pipeline if an error occurs later.

### 2. The `CreditUsage` table is append-only

Never `UPDATE` or `DELETE` rows in `credit_usage`. It is a financial audit ledger. Every ingestion adds a new row — including re-ingestions.

### 3. `SpeakerAnalytic.user_id` is `NULL` by design

The diarization service fills it in later by matching speaker labels to actual users. Don't treat NULL `user_id` as a bug.

### 4. `meeting_graph_id` is the dedup key for meetings

Not `join_url`. The join URL can change if a meeting is rescheduled. The `meeting_graph_id` (Graph's internal ID) is stable.

### 5. Speaker analytics come from RAW segments, not merged chunks

If you change the pipeline to compute speaker stats from merged segments, you'll over-count talk time (gaps between segments would be included).

### 6. `EMBEDDING_DIM = 1536` must match the DB schema

If you change the Azure OpenAI deployment to a model with different output dimensions, update both `EMBEDDING_DIM` in `embedder.py` AND the `Vector(1536)` in `app/db/tenant/models.py → Chunk.embedding`, plus run a DB migration.

### 7. `graph_token` vs the JWT in the `Authorization` header — these are different tokens

The `Authorization` header JWT is used by the backend to authenticate the user (validated by `get_current_user`). The `graph_token` in the request body is a **separate** token that the backend uses to call Microsoft Graph on the user's behalf. They may be the same token if the frontend requests the right scopes, but treat them as separate until CONTEXT.md Open Question #1 is resolved.

---

## 9. Tests

### Running the tests

```bash
pip install pytest pytest-asyncio pytest-cov
python -m pytest tests/ -v
```

Expected: **77 passed** in ~2 seconds. No Azure credentials, DB, or Redis needed.

### Test structure

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_vtt_parser.py` | 25 | `_ts_to_ms`, `parse_vtt` — all timestamp formats, speaker fallback, CRLF, multi-line, empty cues |
| `tests/test_chunker.py` | 27 | `merge_speaker_turns` — gap logic, different speakers, overlaps; `chunk_segments` — short/long/split/timestamps |
| `tests/test_ingestion.py` | 25 | `run_ingestion_pipeline` — happy path, not found, empty VTT, embed failure, re-ingestion; `embed_batch`, `embed_single`, retry on 429 |

### How mocking works

Tests use `unittest.mock.patch` to replace Azure OpenAI calls and SQLAlchemy sessions. No real infrastructure required.

`tests/conftest.py` injects all 16 required environment variables before each test using a `monkeypatch` fixture. This prevents `pydantic_settings` from raising `ValidationError` when `get_settings()` is called during module import.

```python
# conftest.py — runs before every test automatically
@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    # ... all 16 vars
    get_settings.cache_clear()  # clear lru_cache so new values take effect
```

---

## 10. Open Questions

These are from CONTEXT.md and are **not resolved in this PR**. Do not implement assumptions around them.

| # | Question | How it affects this code |
|---|----------|--------------------------|
| **1** | OBO flow vs frontend-passed Graph token | When resolved, only the `gc = GraphClient(body.graph_token)` line in `ingest.py` changes |
| **2** | Key Vault secret naming convention — `db-{org_name}`? | Does not affect ingestion service |
| **3** | Database name convention on PostgreSQL server | Does not affect ingestion service |

---

*For the overall platform architecture, see [CONTEXT.md](../CONTEXT.md).*  
*For DB models, see [db-layer-implementation.md](db-layer-implementation.md).*
