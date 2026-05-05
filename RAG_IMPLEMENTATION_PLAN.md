# RAG v3 — Implementation Plan

This is the design contract for the RAG/chat layer rebuild. Captures every
decision we made during planning so any contributor can pick up the work
without reverse-engineering chat logs.

---

## 1. Scope & non-goals

**In scope (v1):**
- Chat over the user's meetings from the **last 30 days**.
- Multi-meeting selection via left-menu checkboxes.
- Time-range presets (last week / last month / last N days / custom).
- Restricted bot — only answers meeting-related questions.
- Multi-source routing (metadata, insights, chunks, optional general knowledge).
- Comparison between selected meetings via summaries.
- Speaker disambiguation via `meeting_participants` emails.

**Out of scope (v1):**
- Reranker stage (no Cohere / cross-encoder).
- Streaming responses.
- Multi-tab session restoration / named sessions.
- Two-stage retrieval funnel (`meeting_summaries` first-pass).
- Any meeting older than 30 days.
- Eval harness (manual smoke tests only).

---

## 2. Locked decisions

| Area | Decision |
|---|---|
| **Scope default** | Last meeting auto-checked on first load. |
| **Selection persistence** | Persists across queries. Bot asks permission to change scope when query implies different meetings. |
| **Time-range presets** | Each preset has its own checkbox — selecting it scopes search to all meetings in that range. Expanding a preset reveals individual meetings inside. |
| **Routing** | LLM router (`gpt-4.1-mini`). Multi-source per query allowed. |
| **Restrictiveness** | `OFF_TOPIC` queries refused with a soft + example message. `MEETING_PLUS_GK` queries (e.g. "how can we improve our standups") allowed — bot uses general knowledge layered on retrieved meeting context. |
| **Access-type filter** | Toggle in left menu: meetings I attended (`role IN ('organizer','attendee')`) / all access / admin-granted only (`role='granted'`). |
| **Conversation memory** | Last N turns verbatim + structured session state (current scope, last intent, last referenced meeting). |
| **Cross-refresh persistence** | None for v1 — fresh tab = fresh session. |
| **Scope-change UX** | Bot answers with current scope, then surfaces a banner suggesting expansion ("Want me to also include Acme Q3 review?"). On Yes → checkboxes update → query re-runs. |
| **Large-scope output** | Threshold-based: ≤5 meetings full per-meeting detail / 6-15 top-K + "show more" / 16+ ask user to narrow first. |
| **Sources display** | Clean prose answer + collapsible "Sources" section below with one card per meeting referenced (title, date, time spans, jump-to-time link). |
| **Streaming** | Wait-and-show for v1. |
| **Speaker attribution** | Per-turn from `chunk_text[].n`. Never blend two speakers' words within one chunk. |
| **Non-ready meetings** | Faded / non-clickable in left menu — can't be selected. |
| **Off-topic phrasing** | "I'm a meeting assistant — I can't answer that. But I can help you find what was discussed in your meetings, who attended, action items, decisions, etc." |
| **Out-of-30d query** | Soft: "That's outside the last-30-days window I can search. Want me to search the latest 30 days for similar content?" |
| **Cross-meeting speaker reference** | Pre-check at router: scan all 30-day meetings (not just selected) for the speaker. If found outside selection → suggest scope expansion. |
| **Same-name speakers** | Disambiguate using `participant_email` ("Did you mean ashish.jaiswal@vrize.com or ashish.kumar@vrize.com?"). |
| **Retrieval** | Single-stage hybrid (BM25 + cosine + RRF) + round-robin diversification across meetings. |
| **Reranker** | None for v1. |
| **STRUCTURED route** | Two sub-modes — `STRUCTURED_DIRECT` (format insights, no LLM, fast) for "list X" queries; `STRUCTURED_LLM` (narrative composition) for "summarise". |
| **`meeting_summaries` table** | Keep — primary consumer is the COMPARE handler. |
| **Models** | `gpt-4o` for everything by default, `gpt-4.1-mini` for the router. Per-task overrides via `.env`. |
| **Latency P95** | <5 seconds end-to-end. |
| **Scale (1-3 mo)** | 100-200 users tenant-wide, ~500 queries/day total. No queueing/sharding needed. |
| **Cost** | Not optimised for v1. |
| **Caching** | Per-request memoized RBAC + in-process LRU for speaker→graph_id lookups. No Redis cache yet. |
| **Tests** | Unit tests per handler with fake repos. Eval harness deferred. |

---

## 3. Architecture

### 3.1 Backend file structure (aligned with existing va-test-2 layout)

```
app/
├── api/routes/
│   └── chat.py                          [NEW]  POST /chat endpoint, owns the transaction
├── db/helpers/
│   └── chat_search.py                   [NEW]  hybrid SQL + round-robin diversification
└── services/
    ├── llm/                             [NEW]
    │   ├── client.py                    AzureOpenAIClient (lru_cache singleton)
    │   └── deployments.py               llm_for_router/answer/insights/summary — read .env with overrides
    └── chat/                            [NEW]
        ├── interfaces.py                Protocol classes — MetadataRepo, InsightsRepo, ChunkSearcher,
        │                                LLMClient, SessionStore (for testability + future swap)
        ├── orchestrator.py              entry: handle_chat() — wires router → scope → handler → answer → sources
        ├── router.py                    classify_query() → RouterDecision (calls LLM + speaker resolver)
        ├── scope.py                     resolve_scope(), narrow_within_scope(), detect_scope_change()
        ├── session.py                   in-memory SessionStore (dict-backed; swap to Redis later)
        ├── answer.py                    _build_context() per route + generate_answer() LLM call
        ├── sources.py                   _build_sources() — UI source-card payloads
        ├── handlers/
        │   ├── meta.py                  meetings/transcripts/participants queries
        │   ├── structured_direct.py     format insights as bullets, no LLM
        │   ├── structured_llm.py        LLM narrative summary using all insight fields
        │   ├── search.py                hybrid + round-robin chunk search
        │   ├── hybrid.py                structured + search in parallel via asyncio.gather
        │   ├── compare.py               cross-meeting comparison via meeting_summaries + insights
        │   └── general.py               OFF_TOPIC refusal OR MEETING_PLUS_GK reasoning
        └── prompts/
            ├── __init__.py              ROUTE_PROMPTS dict
            ├── router.py                ROUTER_SYSTEM
            ├── search.py                SEARCH_SYSTEM
            ├── structured.py            STRUCTURED_SYSTEM (LLM mode)
            ├── hybrid.py                HYBRID_SYSTEM
            ├── compare.py               COMPARE_SYSTEM
            ├── meta.py                  META_SYSTEM
            └── general.py               GENERAL_REFUSAL + GENERAL_GK_SYSTEM
```

### 3.2 Frontend file structure (`Video-Analytics-UI`)

```
app/
├── chat/page.tsx                        [NEW]  primary chat surface
└── ingest/page.tsx                      [keeps] existing meeting-ingest UI

components/chat/                         [NEW]
├── Sidebar.tsx                          left rail with time-range filters + meeting list
├── MeetingItem.tsx                      one row: checkbox, title, date, role badge,
│                                        faded when status != 'ready'
├── TimeRangeSection.tsx                 collapsible group with its own scope-checkbox
├── ChatMessages.tsx                     message list (no streaming for v1)
├── ChatInput.tsx                        composer
├── SourcesCard.tsx                      collapsible "Sources" panel below each answer
└── ScopeBanner.tsx                      "Want to expand scope to X meetings? [Yes][No]"
```

### 3.3 Routing decision contract

The router LLM emits this exact shape:

```python
{
  "route": "META" | "STRUCTURED_DIRECT" | "STRUCTURED_LLM" |
           "SEARCH" | "HYBRID" | "COMPARE" |
           "GENERAL_REFUSE" | "GENERAL_GK",

  "filters": {
    "speaker_name": str | None,            # raw name as it appeared in the query
    "speaker_graph_ids": list[str] | None, # resolved by tenant-wide participants lookup
    "speaker_disambiguation_needed": bool,
    "speaker_candidates": list[{           # only when 2+ same-name people match
        "name": str,
        "email": str,
        "graph_id": str
    }] | None,
    "date_from": str | None,               # ISO YYYY-MM-DD
    "date_to":   str | None,               # ISO YYYY-MM-DD
    "meeting_titles": list[str] | None,    # for narrow-within-scope
    "keyword_focus": str | None,
  },

  "scope_intent": {
    "needs_change": bool,
    "suggested_meeting_ids": list[str] | None,
    "reason": str,                         # human-readable for the banner
  },

  "out_of_30_day_window": bool,            # query references a date older than 30 days
  "search_query": str,                     # cleaned, embedding-optimised reformulation
}
```

### 3.4 Intent → source mapping

| Intent example | Route | Source(s) |
|---|---|---|
| "List my meetings", "who attended X" | `META` | `meetings`, `meeting_participants`, `transcripts` |
| "List action items / decisions / follow-ups" | `STRUCTURED_DIRECT` | `meeting_insights` (raw, formatted as bullets, no LLM) |
| "Summarise X", "tl;dr", "gist" | `STRUCTURED_LLM` | `meeting_insights` (all fields) → LLM narrative |
| "What did X say about Y", "did we discuss Z" | `SEARCH` | `chunks` hybrid + round-robin |
| "Summarise X and quote the discussion about Y" | `HYBRID` | `meeting_insights` + `chunks` |
| "Compare meeting A vs meeting B" | `COMPARE` | `meeting_summaries.summary_text` + `meeting_insights` per meeting |
| "What's the capital of France" | `GENERAL_REFUSE` | none — refusal |
| "How can we improve our standups" | `GENERAL_GK` | `chunks` for evidence + LLM general knowledge |

---

## 4. Two special requirements

### 4.1 Comparison between meetings

**Triggers:** query contains *"compare", "vs", "difference between", "what changed", "in both meetings"* and references ≥2 meetings.

**Handler `compare.py`:**

1. Resolve which meetings are being compared (from `filters.meeting_titles` or explicit "these 3 meetings").
2. Pull `meeting_summaries.summary_text` AND all `meeting_insights` (summary, action_items, key_decisions, follow_ups) for each compared meeting. **No chunk search by default** — comparisons work better at the summary level.
3. Build LLM context with one labelled section per meeting:
    ```
    === Meeting A: <title> · <date> ===
    Summary: ...
    Decisions: ...
    Action items: ...

    === Meeting B: <title> · <date> ===
    ...
    ```
4. LLM with `COMPARE_SYSTEM` prompt produces a structured comparison: common ground, divergences, what's new in B, what was dropped from A, etc.
5. If the query also asks for specific quotes ("compare what Sarah said about pricing"), fall back to `HYBRID` with `compare_mode=True` (returns chunks per meeting separately).

This is why we keep the `meeting_summaries` table — it's the comparison-handler's primary input.

### 4.2 Narrow-within-scope

**Trigger:** user has multiple meetings selected but the query references specific ones by title or date.

**Example:** 5 meetings selected, user asks *"what was decided in the Acme renewal review?"*.

The router fills `filters.meeting_titles=["Acme renewal review"]`. Then `scope.narrow_within_scope()` runs:

```python
def narrow_within_scope(
    selected_ids: list[UUID],
    requested_titles: list[str] | None,
    requested_dates: tuple[date, date] | None,
    db: AsyncSession,
) -> NarrowResult:
    """
    Returns:
      matched_ids: subset of selected_ids that match the request
      extra_ids:   meetings matching the request but NOT in selected (need scope expansion)
      dropped:     selected meetings irrelevant to this query (excluded from search)
    """
```

**Decision logic:**
- All requested ⊆ selected → narrow silently. Answer covers only those. UI shows a small badge: *"Searched: 2 of 5 selected meetings."*
- Some requested ⊄ selected → trigger scope-change suggestion. Answer with current matches only, then *"Want to also include Acme Q3 review (not in your selection)?"*

Handlers always receive `effective_meeting_ids`, never the raw selection.

---

## 5. Build phases

### Phase 1 — Foundation

**Files:**
- `app/services/llm/deployments.py`
- `app/services/llm/client.py`
- `app/services/chat/interfaces.py`
- `app/services/chat/session.py`

**Definition of Done:**
- `from app.services.llm.deployments import llm_for_router; llm_for_router()` returns the configured deployment, falls back correctly when override env-var unset.
- `SessionStore` in-memory passes unit tests for: `get_scope()`, `set_scope()`, `set_last_referenced_meeting()`, last-N turns rolling window.
- All Protocol classes in `interfaces.py` defined with full type hints. No implementations yet.

### Phase 2 — Repos & retrieval

**Files:**
- `app/db/helpers/chat_search.py` — `hybrid_chunk_search(query_emb, query_text, meeting_ids, db)` with round-robin
- Inline metadata + insights query helpers in handlers (or separate `repos/` if it grows)

**DoD:**
- `hybrid_chunk_search` returns deduped, RRF-ranked, round-robin-distributed chunks.
- Test fixture: 3 meetings × 5 chunks each → query → assert all 3 meetings represented in top-10, top chunks are highest RRF.
- Insights-fetch helper merges `meeting_insights` rows per meeting_id and unwraps the `{"items": [...]}` JSONB shape to a clean Python list.

### Phase 3 — Router, scope, handlers, prompts

**Files:**
- `app/services/chat/router.py`
- `app/services/chat/scope.py`
- `app/services/chat/handlers/*.py` (all 7)
- `app/services/chat/prompts/*.py` (all 7)

**DoD:**
- Each handler is independently unit-testable with fake repos + fake LLM.
- Router test: 30 hand-curated `(query, expected_route, expected_filter_keys)` tuples covering each route. ≥27/30 pass.
- All ContextIQ prompt patterns I bring in are noted in module-level docstrings (speaker accuracy block, anti-hallucination guards, MoM structure, meeting-calendar header).
- `narrow_within_scope()` covers all three branches (all-in, some-extra, all-extra).

### Phase 4 — Orchestrator, endpoint, answer composition

**Files:**
- `app/services/chat/orchestrator.py`
- `app/services/chat/answer.py`
- `app/services/chat/sources.py`
- `app/api/routes/chat.py`
- Wire into `app/main.py` (chat_router include)

**DoD:**
- POST `/chat` with a real session → returns answer + sources + scope-change suggestion if relevant.
- Backend smoke test against `va_alpha_v2`: ingested meetings → 5 hand-written queries → all return non-empty, sensible answers within 5s P95.
- All edge cases from §2 produce expected output (off-topic refusal, out-of-30d soft suggestion, scope-expansion suggestion, faded non-ready meetings excluded).

### Phase 5 — Frontend chat surface

**Files (in `Video-Analytics-UI`):**
- `app/chat/page.tsx`
- `components/chat/Sidebar.tsx`
- `components/chat/MeetingItem.tsx`
- `components/chat/TimeRangeSection.tsx`
- `components/chat/ChatMessages.tsx`
- `components/chat/ChatInput.tsx`
- `components/chat/SourcesCard.tsx`
- `components/chat/ScopeBanner.tsx`

**DoD:**
- Sign in → land on chat → last meeting auto-selected → ask a question → answer renders with sources card → click Sources to expand → see meeting/time spans.
- Scope-change suggestion appears as a banner under the answer; clicking Yes updates checkboxes and re-runs.
- Time-range section selectable: clicking the section's checkbox selects all meetings in that range; expanding reveals individual meeting checkboxes.
- Non-`ready` meetings rendered faded and uncheckable.

### Phase 6 — Post-demo polish (deferred)

- Eval harness with a fixed test set, run on prompt changes
- Redis-backed `SessionStore` (swap implementation, keep interface)
- Reranker stage (Cohere or local cross-encoder) — added behind a feature flag
- Two-stage retrieval funnel via `meeting_summaries` for large scopes
- Streaming responses (SSE)
- Multi-named sessions in left rail (3b option D)

---

## 6. Prompts — patterns from ContextIQ

We re-use these patterns from `c:/Users/Ashish Jaiswal/Documents/va-dev/ContextIQ-main`:

| Pattern (source file) | Where it lands in v3 |
|---|---|
| **`rag_service.py`** — "INDEXED MEETINGS:" calendar block at top of system prompt | `prompts/search.py`, `prompts/hybrid.py`, `prompts/structured.py`, `prompts/meta.py`, `prompts/compare.py` |
| **`rag_service.py`** — numbered SPEAKER ACCURACY RULES (CRITICAL) A-D | `prompts/search.py`, `prompts/hybrid.py`, `prompts/compare.py` |
| **`rag_service.py`** — *"If not found, say exactly: <text>"* discipline | All meeting-data route prompts |
| **`rag_service.py`** — "Don't embed timestamps/inline citations in prose" | All meeting-data route prompts (sources go in UI cards) |
| **`insights_service.py`** — comprehensive extraction (assigned_to, priority, context, alternatives, dependencies) | Already brought into `app/services/insights/prompts.py` |
| **`summary_service.py`** — per-speaker + overall meeting summary structure | Already brought into `pipeline.py` `_SUMMARY_SYSTEM_PROMPT` |

The router prompt and the COMPARE prompt are net-new in v3.

---

## 7. Implementation discipline

- **Async everywhere**: every DB call, every HTTP call, every LLM call uses `await`. No blocking I/O in request handlers.
- **Type hints everywhere**: every public function and class has full type annotations. `mypy --strict` should pass on `app/services/chat/` and `app/services/llm/`.
- **Unit-testable handlers**: every handler accepts its dependencies via Protocol parameters, never reaches into globals or `app.state`. Tests construct handlers with fake repos + fake LLM clients.
- **One transaction per request**: chat endpoint owns the transaction. Handlers and repos never call `db.commit()`. Errors → rollback in middleware.
- **No hidden network calls**: no LLM call buried inside a "format" function. The LLM call site is always visible at handler level.
- **`.env` for everything swappable**: model deployments, similarity threshold, RRF constant, top-K, retrieval pool size, max history turns. No magic numbers in code.

---

## 8. Open questions (TBD during implementation)

- Exact UI for time-range section: separate scope-checkbox vs. clicking the section header. Clarify with design pass on Sidebar.tsx.
- Source-card click behaviour: jump to a video player at the timestamp? Open meeting page? Decide when SourcesCard.tsx is built.
- Whether `MEETING_PLUS_GK` route needs chunks at all, or can answer purely from insights + GK. Decide during Phase 3 prompt drafting.
- Compare-handler fall-through: when summaries are missing for some compared meetings (insight gen failed), should we fall back to chunks or refuse? Decide during Phase 3.

---

## 9. References

- **ContextIQ-main**: `c:/Users/Ashish Jaiswal/Documents/va-dev/ContextIQ-main` — prompt-pattern source.
- **Existing v2 ingestion code**: `app/services/ingestion/`, `app/services/insights/` — kept as-is.
- **Schema**: `app/db/tenant/models.py` — multi-turn `chunks`, `meeting_insights`, `meeting_summaries` already in place.

---

*Plan locked 2026-05-04. Ready to execute Phase 1 on go-ahead.*
